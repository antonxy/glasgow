from amaranth import *
from amaranth.lib import io
from amaranth.sim import Simulator

from glasgow.support import logging
from glasgow.simulation.assembly import SimulationAssembly
from glasgow.gateware.ports import PortGroup
from glasgow.gateware.stream import stream_get
from glasgow.applet import GlasgowAppletV2TestCase, synthesis_test
from . import TDMFrontend, TDMInterface, TDMApplet


logger = logging.getLogger(__name__)


class TDMAppletTestCase(GlasgowAppletV2TestCase, applet=TDMApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds([])

    def tdm_testbench(self, ports):
        async def testbench(ctx):
            frames = [
                [0,1,0,1,0,1,0,1,0,0,0,0,0,0,0,0],
                [0,1,0,1,0,1,0,1,0,0,0,0,0,0,0,1],
                [0,1,0,1,0,1,0,1,0,0,0,0,0,0,1,0],
                [0,1,0,1,0,], #incomplete frame
                [0,1,0,1,0,1,0,1,0,0,0,0,0,0,1,1], #this frame will be skipped
                [0,1,0,1,0,1,0,1,0,0,0,0,0,1,0,0],
            ]
            half_cycle = .5/(48000*32*2)

            # Cycle bclk a few times before first fsync
            for _ in range(10):
                ctx.set(ports.bclk.i, 0)
                await ctx.delay(half_cycle)
                ctx.set(ports.bclk.i, 1)
                await ctx.delay(half_cycle)

            for frame in frames:
                first_bit = True
                for rx in frame:
                    await ctx.delay(half_cycle)
                    ctx.set(ports.fsync.i, 1 if first_bit else 0)
                    ctx.set(ports.bclk.i, 0)
                    ctx.set(ports.rx.i, rx)
                    await ctx.delay(half_cycle)
                    ctx.set(ports.bclk.i, 1)
                    first_bit = False
        return testbench

    def test_sim_frontend(self):
        ports = PortGroup()
        ports.bclk  = io.SimulationPort("i", 1, name="bclk")
        ports.fsync = io.SimulationPort("i", 1, name="fsync")
        ports.rx    = io.SimulationPort("i", 1, name="rx")

        dut = TDMFrontend(ports, 2)

        async def stream_testbench(ctx):
            words = [
                0b01010101, 0b00000000,
                0b01010101, 0b00000001,
                0b01010101, 0b00000010,
                None, None, # invalid data
                0b01010101, 0b00000100,
            ]
            for word in words:
                received = await stream_get(ctx, dut.rx_stream)
                if word is not None:
                    self.assertEqual(word, received)
                print("{:08b}".format(received))

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(self.tdm_testbench(ports))
        sim.add_testbench(stream_testbench)
        with sim.write_vcd("test_spi_analyzer_frontend.vcd"):
            sim.run()