import argparse
from amaranth import *
from amaranth.lib import wiring, stream, io
from amaranth.lib.wiring import Out, In

from glasgow.support import logging
from glasgow.gateware.stream import AsyncQueue
from glasgow.gateware.iostream import SimulatableDDRBuffer
from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletV2

__all__ = ["PDMMicComponent", "PDMMicApplet"]

class ClockDriveComponent(wiring.Component):
    enabled: In(1)
    divisor: In(16)
    pdmclk: Out(1)

    def __init__(self, ports):
        self._ports = ports

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.clk_buffer = clk_buffer = io.FFBuffer("o", self._ports.clk)

        m.d.comb += clk_buffer.oe.eq(self.enabled)

        timer = Signal.like(self.divisor)
        with m.If(self.enabled):
            with m.If(timer == 0):
                m.d.sync += timer.eq(self.divisor)
                m.d.sync += clk_buffer.o.eq(~clk_buffer.o)
            with m.Else():
                m.d.sync += timer.eq(timer - 1)
        with m.Else():
            m.d.sync += timer.eq(self.divisor)
            m.d.sync += clk_buffer.o.eq(0)

        m.d.comb += self.pdmclk.eq(clk_buffer.o)
        # TODO generate a second clock output that is shifted by a quater cycle for sampling

        return m

class DDRShifter(wiring.Component):
    enabled: In(1)
    i: In(2)
    o: Out(stream.Signature(8))

    def elaborate(self, platform):
        m = Module()

        shreg = Signal(8)
        count = Signal(range(8))

        with m.If(self.enabled):
            m.d.sync += shreg.eq(Cat(self.i, shreg))
            m.d.sync += count.eq(count + 1)

            with m.If(count == 7):
                m.d.comb += self.o.valid.eq(1)
                m.d.comb += self.o.payload.eq(shreg)
        with m.Else():
            m.d.sync += count.eq(0)

        return m

class PDMMicComponent(wiring.Component):
    o_stream: Out(stream.Signature(8))

    clock_enabled: In(1)
    divisor: In(16)
    capture_enabled: In(1)

    def __init__(self, ports):
        self._ports = ports

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.clockgen = clockgen = ClockDriveComponent(self._ports)
        m.d.comb += clockgen.enabled.eq(self.clock_enabled)
        m.d.comb += clockgen.divisor.eq(self.divisor)

        m.domains.pdmclk = cd_pdmclk = ClockDomain("pdmclk")
        m.d.sync += cd_pdmclk.clk.eq(clockgen.pdmclk),
        # TODO check sampling position relative to clock
        # Maybe I have to generate a second clock with phase offset for the sampling
        m.submodules.dat_buffer = dat_buffer = SimulatableDDRBuffer(direction='i', port=self._ports.dat, i_domain="pdmclk")

        m.submodules.shifter = shifter = DomainRenamer("pdmclk")(DDRShifter())
        m.d.comb += shifter.enabled.eq(self.capture_enabled)
        m.d.comb += shifter.i.eq(dat_buffer.i)

        m.submodules.cdc_fifo = cdc_fifo = AsyncQueue.shaped_like(self.o_stream,
            depth=4,
            i_domain="pdmclk",
            o_domain="sync",
        )
        wiring.connect(m, shifter.o, cdc_fifo.i)
        wiring.connect(m, wiring.flipped(self.o_stream), cdc_fifo.o)


        return m


class PDMMicInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
                 clk: GlasgowPin, dat: GlasgowPin):
        self._logger = logger
        self._level  = logging.DEBUG if self._logger.name == __name__ else logging.TRACE

        ports = assembly.add_port_group(clk=clk, dat=dat)
        component = assembly.add_submodule(PDMMicComponent(ports))
        self._pipe = assembly.add_in_pipe(component.o_stream)
        self._clock_enabled = assembly.add_rw_register(component.clock_enabled)
        self._capture_enabled = assembly.add_rw_register(component.capture_enabled)
        self._clock = assembly.add_clock_divisor(component.divisor,
            ref_period=assembly.sys_clk_period * 2, name="pdm_clk")

    def _log(self, message, *args):
        self._logger.log(self._level, "PDM: " + message, *args)

    async def capture(self, n_bytes):
        await self._clock.set_frequency(3_072_000)
        await self._clock_enabled.set(True)
        await self._capture_enabled.set(True)
        cap = (await self._pipe.recv(n_bytes)).tobytes()
        await self._capture_enabled.set(False)
        # TODO how do I flush the i-pipe?
        await self._clock_enabled.set(False)
        return cap



class PDMMicApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "Capture the raw bitstream from a PDM microphone"
    description = """
    """
    # Not tested on anything else
    required_revision = "C0"

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)
        access.add_pins_argument(parser, "clk",  required=True, default=True)
        # TODO allow multple data pins
        access.add_pins_argument(parser, "dat", required=True, default=True)

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.pdm_iface = PDMMicInterface(self.logger, self.assembly,
                clk=args.clk, dat=args.dat,
            )

    @classmethod
    def add_setup_arguments(cls, parser):
        parser.add_argument(
            "-f", "--clk-freq", metavar="FREQ", type=float,
            help="generate PDM CLK with FREQ Hz")
    
    def setup(self, args):
        self.pdm_iface._clock.set_frequency(args.clk_freq)

    @classmethod
    def add_run_arguments(cls, parser):
        # TODO make optional (record / send clock until Ctrl-C)
        parser.add_argument(
            "-d", "--duration", type=float,
            help="Recording duration in sec")

        # TODO make recording optional
        # have one output file per channel?
        parser.add_argument(
            "FILE", type=argparse.FileType('wb'),
            help="bitstream file")
        pass

    async def run(self, args):
        # TODO record data
        pass

    @classmethod
    def tests(cls):
        from . import test
        return test.PDMMicAppletTestCase
