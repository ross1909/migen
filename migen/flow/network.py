from networkx import MultiDiGraph

from migen.fhdl.structure import *
from migen.flow.actor import *
from migen.flow import plumbing
from migen.corelogic.misc import optree

# Graph nodes can be either:
#  (1) a reference to an existing actor
#  (2) an abstract (class, dictionary) pair meaning that the actor class should be
#      instantiated with the parameters from the dictionary.
#      This form is needed to enable actor duplication or sharing during elaboration.

class ActorNode:
	def __init__(self, actor_class, parameters=dict()):
		if isinstance(actor_class, type):
			self.actor_class = actor_class
			self.parameters = parameters
		else:
			self.actor = actor_class
		self.name = None
	
	def is_abstract(self):
		return hasattr(self, "actor_class")
		
	def instantiate(self):
		self.actor = self.actor_class(**self.parameters)
		self.actor.name = self.name
		del self.actor_class
		del self.parameters
	
	def get_dict(self):
		if self.is_abstract():
			return self.parameters
		else:
			return self.actor.__dict__
	
	def __repr__(self):
		if self.is_abstract():
			r = "<abstract " + self.actor_class.__name__
			if self.name is not None:
				r += ": " + self.name
			r += ">"
		else:
			r = repr(self.actor)
		return r

class DataFlowGraph(MultiDiGraph):
	def __init__(self):
		self.elaborated = False
		super().__init__()
	
	def add_connection(self, source_node, sink_node,
	  source_ep=None, sink_ep=None,		# default: assume nodes have 1 source/sink and use that one
	  source_subr=None, sink_subr=None):	# default: use whole record
		assert(isinstance(source_node, ActorNode))
		assert(isinstance(sink_node, ActorNode))
		self.add_edge(source_node, sink_node,
			source=source_ep, sink=sink_ep,
			source_subr=source_subr, sink_subr=sink_subr)
	
	def del_connections(self, source_node, sink_node, data_requirements):
		edges_to_delete = []
		edge_data = self.get_edge_data(source_node, sink_node)
		if edge_data is None:
			# the two nodes are already completely disconnected
			return
		for key, data in edge_data.items():
			if all(k not in data_requirements or data_requirements[k] == v
			  for k, v in data.items()):
				edges_to_delete.append(key)
		for key in edges_to_delete:
			self.remove_edge(source_node, sink_node, key)
	
	# Returns a dictionary
	#   source -> [sink1, ..., sinkn]
	# source element is a (node, endpoint) pair.
	# sink elements are (node, endpoint, source subrecord, sink subrecord) triples.
	def _source_to_sinks(self):
		d = dict()
		for u, v, data in self.edges_iter(data=True):
			el_src = (u, data["source"])
			el_dst = (v, data["sink"], data["source_subr"], data["sink_subr"])
			if el_src in d:
				d[el_src].append(el_dst)
			else:
				d[el_src] = [el_dst]
		return d
	
	# Returns a dictionary
	#   sink -> [source1, ... sourcen]
	# sink element is a (node, endpoint) pair.
	# source elements are (node, endpoint, sink subrecord, source subrecord) triples.
	def _sink_to_sources(self):
		d = dict()
		for u, v, data in self.edges_iter(data=True):
			el_src = (u, data["source"], data["sink_subr"], data["source_subr"])
			el_dst = (v, data["sink"])
			if el_dst in d:
				d[el_dst].append(el_src)
			else:
				d[el_dst] = [el_src]
		return d
	
	# List sources that feed more than one sink.
	def _list_divergences(self):
		d = self._source_to_sinks()
		return dict((k, v) for k, v in d.items() if len(v) > 1)

	# A graph is abstract if any of these conditions is met:
	#  (1) A node is an abstract actor.
	#  (2) A subrecord is used.
	#  (3) A single source feeds more than one sink.
	# NB: It is not allowed for a single sink to be fed by more than one source
	# (except with subrecords, i.e. when a combinator is used)
	def is_abstract(self):
		return any(x.is_abstract() for x in self) \
			or any(d["source_subr"] is not None or d["sink_subr"] is not None
				for u, v, d in self.edges_iter(data=True)) \
			or bool(self._list_divergences())
	
	def _eliminate_subrecords_and_divergences(self):
		# Insert combinators.
		for (dst_node, dst_endpoint), sources in self._sink_to_sources().items():
			if len(sources) > 1 or sources[0][2] is not None:
				# build combinator
				# "layout" is filled in during instantiation
				subrecords = [dst_subrecord for src_node, src_endpoint, dst_subrecord, src_subrecord in sources]
				combinator = ActorNode(plumbing.Combinator, {"subrecords": subrecords})
				# disconnect source1 -> sink ... sourcen -> sink
				# connect source1 -> combinator_sink1 ... sourcen -> combinator_sinkn
				for n, (src_node, src_endpoint, dst_subrecord, src_subrecord) in enumerate(sources):
					self.del_connections(src_node, dst_node,
						{"source": src_endpoint, "sink": dst_endpoint})
					self.add_connection(src_node, combinator,
						src_endpoint, "sink{0}".format(n), source_subr=src_subrecord)
				# connect combinator_source -> sink
				self.add_connection(combinator, dst_node, "source", dst_endpoint)
		# Insert splitters.
		for (src_node, src_endpoint), sinks in self._source_to_sinks().items():
			if len(sinks) > 1 or sinks[0][2] is not None:
				subrecords = [src_subrecord for dst_node, dst_endpoint, src_subrecord, dst_subrecord in sinks]
				splitter = ActorNode(plumbing.Splitter, {"subrecords": subrecords})
				# disconnect source -> sink1 ... source -> sinkn
				# connect splitter_source1 -> sink1 ... splitter_sourcen -> sinkn
				for n, (dst_node, dst_endpoint, src_subrecord, dst_subrecord) in enumerate(sinks):
					self.del_connections(src_node, dst_node,
						{"source": src_endpoint, "sink": dst_endpoint})
					self.add_connection(splitter, dst_node,
						"source{0}".format(n), dst_endpoint)
				# connect source -> splitter_sink
				self.add_connection(src_node, splitter, src_endpoint, "sink")
	
	def _infer_plumbing_layout(self):
		while True:
			ap = [a for a in self if a.is_abstract() and a.actor_class in plumbing.actors]
			if not ap:
				break
			for a in ap:
				if a.actor_class in plumbing.layout_sink:
					edges = self.in_edges(a, data=True)
					assert(len(edges) == 1)
					other, me, data = edges[0]
					if other.is_abstract():
						continue
					other_ep = data["source"]
					if other_ep is None:
						other_ep = other.actor.single_source()
				elif a.actor_class in plumbing.layout_source:
					edges = self.out_edges(a, data=True)
					assert(len(edges) == 1)
					me, other, data = edges[0]
					if other.is_abstract():
						continue
					other_ep = data["sink"]
					if other_ep is None:
						other_ep = other.actor.single_sink()
				else:
					raise AssertionError
				layout = other.actor.token(other_ep).layout()
				a.parameters["layout"] = layout
				a.instantiate()
	
	def _instantiate_actors(self):
		# 1. instantiate all abstract non-plumbing actors
		for actor in self:
			if actor.is_abstract() and actor.actor_class not in plumbing.actors:
				actor.instantiate()
		# 2. infer plumbing layout and instantiate plumbing
		self._infer_plumbing_layout()
		# 3. resolve default eps
		for u, v, d in self.edges_iter(data=True):
			if d["source"] is None:
				d["source"] = u.actor.single_source()
			if d["sink"] is None:
				d["sink"] = v.actor.single_sink()
	
	# Elaboration turns an abstract DFG into a concrete one.
	#   Pass 1: eliminate subrecords and divergences
	#           by inserting Combinator/Splitter actors
	#   Pass 2: run optimizer (e.g. share and duplicate actors)
	#   Pass 3: instantiate all abstract actors and explicit "None" endpoints
	def elaborate(self, optimizer=None):
		if self.elaborated:
			return
		self.elaborated = True
		
		self._eliminate_subrecords_and_divergences()
		if optimizer is not None:
			optimizer(self)
		self._instantiate_actors()

class CompositeActor(Actor):
	def __init__(self, dfg):
		dfg.elaborate()
		self.dfg = dfg
		super().__init__()
	
	def get_fragment(self):
		comb = [self.busy.eq(optree("|", [node.actor.busy for node in self.dfg]))]
		fragment = Fragment(comb)
		for node in self.dfg:
			fragment += node.actor.get_fragment()
		for u, v, d in self.dfg.edges_iter(data=True):
			ep_src = u.actor.endpoints[d["source"]]
			ep_dst = v.actor.endpoints[d["sink"]]
			fragment += get_conn_fragment(ep_src, ep_dst)
		return fragment
