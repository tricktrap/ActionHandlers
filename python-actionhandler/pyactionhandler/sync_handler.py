import gevent
import zmq.green as zmq
from greenlet import GreenletExit
import greenlet
from pyactionhandler.helper import decode_rpc_call
from pyactionhandler.exceptions import DecodeRPCError
from pyactionhandler.protobuf.ActionHandler_pb2 import ActionRequest, ActionResponse
import itertools
import logging

class SyncHandler(object):
	def __init__(self, worker_collection, zmq_url):
		self.logger = logging.getLogger('root')
		self.worker_collection=worker_collection
		self.zmq_url=zmq_url
		self.zmq_ctx = zmq.Context()
		self.zmq_socket = self.zmq_ctx.socket(zmq.ROUTER)
		self.zmq_socket.bind(self.zmq_url)
		self.response_queue=gevent.queue.JoinableQueue(maxsize=0)
		self.worker_collection.register_response_queue(
			self.response_queue)

	def run(self):
		self.input_loop=gevent.spawn(self.handle_requests)
		self.worker_loop=gevent.spawn(self.worker_collection.handle_requests_per_worker)
		self.output_loop=gevent.spawn(self.handle_responses)
		self.counter=itertools.count(start=1, step=1)
		return self.output_loop

	def shutdown(self):
		gevent.kill(self.input_loop)
		gevent.idle()
		self.worker_collection.shutdown_workers()
		self.logger.info("Waiting for all workers to shutdown...")
		while len(self.worker_collection.workers) > 0:
			self.logger.debug("{num} worker(s) still active".format(num=len(self.worker_collection.workers)))
			gevent.sleep(1)
		self.logger.info("Waiting for all responses to be delivered...")
		while self.response_queue.unfinished_tasks > 0:
			self.logger.debug("{num} responses to be delivered".format(num=self.response_queue.unfinished_tasks))
			gevent.sleep(1)
		gevent.kill(self.output_loop)
		self.logger.info("ActionHandler shut down, {num} actions processed".format(num=next(self.counter)-1))

	def next_request(self):
		id1, id2, svc_call, params = self.zmq_socket.recv_multipart()
		try:
			anum=next(self.counter)
			service, method = decode_rpc_call(svc_call)
			req=ActionRequest()
			req.ParseFromString(params)
			params_dict = {param.key: param.value for param in req.params_list}
			self.logger.debug("[{anum}] Decoded RPC message".format(anum=anum))
			return (anum,
					req.capability,
					req.time_out,
					params_dict,
					(id1, id2, svc_call))
		except (DecodeRPCError)  as e:
			self.logger.error("Could not decode RPC message")
			raise

	def handle_requests(self):
		try:
			self.logger.info("Started handling requests")
			while True:
				if self.worker_collection.task_queue.unfinished_tasks >= self.worker_collection.parallel_tasks:
					gevent.sleep(1)
					continue
				try:
					anum, capability, timeout, params, zmq_info = self.next_request()
				except (DecodeRPCError):
					continue
				self.worker_collection.task_queue.put(
					(anum, capability, timeout, params, zmq_info))
				self.logger.debug(
					"[{anum}] Put Action on ActionHandler request queue".format(anum=anum))
		except GreenletExit as e:
			## Block all further incoming messages
			self.logger.info("Stopped handling requests")

	def handle_responses(self):
		try:
			self.logger.info("Started handling responses")
			while True:
				action=self.response_queue.get()
				id1, id2, svc_call = action.zmq_info
				resp=ActionResponse()
				resp.output = action.output
				resp.error_text = action.error_output
				resp.system_rc = action.system_rc
				resp.statusmsg = action.statusmsg
				resp.success = action.success
				self.zmq_socket.send_multipart((id1, id2, svc_call, resp.SerializeToString()))
				#self.worker_collection.task_queue.task_done()
				del id1, id2, svc_call, resp
				self.response_queue.task_done()
				self.logger.debug("[{anum}] Removed Action from ActionHandler response queue".format(
					anum=action.num))
		except GreenletExit as e:
			self.logger.info("Stopped handling responses")

