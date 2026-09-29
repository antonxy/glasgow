from amaranth import *

from glasgow.support import logging
from glasgow.simulation.assembly import SimulationAssembly
from glasgow.gateware.ports import PortGroup
from glasgow.applet import GlasgowAppletV2TestCase, synthesis_test
from . import PDMMicInterface, PDMMicApplet
import itertools


logger = logging.getLogger(__name__)


class PDMMicAppletTestCase(GlasgowAppletV2TestCase, applet=PDMMicApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds([])

    def pdm_testbench(self, ports):
        async def testbench(ctx):
            data = itertools.cycle([0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 1, 0, 0, 1, 1])
            last_clk = 0
            while True:
                await ctx.tick()
                clk = ctx.get(ports.clk.o)
                if clk != last_clk:
                    await ctx.delay(.25e-6)
                    ctx.set(ports.dat.i, next(data))
                    last_clk = clk
        return testbench

    def test_sim_interface(self):
        assembly = SimulationAssembly()

        iface = PDMMicInterface(logger, assembly, clk="A0", dat="A1")

        ports = PortGroup()
        ports.clk = assembly.get_pin("A0")
        ports.dat = assembly.get_pin("A1")
        assembly.add_testbench(self.pdm_testbench(ports))

        async def iface_testbench(ctx):
            print(await iface.capture(8))
            raise StopIteration()

        assembly.run(iface_testbench, vcd_file="test_pdm_mic_interface.vcd")