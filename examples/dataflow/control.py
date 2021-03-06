from migen.flow.network import *
from migen.actorlib import control
from migen.actorlib.sim import *
from migen.sim.generic import Simulator
from migen.sim.icarus import Runner

def source_gen():
	for i in range(10):
		v = i + 5
		print("==> " + str(v))
		yield Token("source", {"value": v})

def sink_gen():
	while True:
		t = Token("sink")
		yield t
		print(t.value["value"])

def main():
	source = ActorNode(SimActor(source_gen(), ("source", Source, [("value", BV(32))])))
	loop = ActorNode(control.For(32))
	sink = ActorNode(SimActor(sink_gen(), ("sink", Sink, [("value", BV(32))])))
	g = DataFlowGraph()
	g.add_connection(source, loop)
	g.add_connection(loop, sink)
	comp = CompositeActor(g)
	fragment = comp.get_fragment()
	sim = Simulator(fragment, Runner())
	sim.run(500)

main()
