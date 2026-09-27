import argparse
import asyncio
from amaranth import *
from amaranth.lib import data, wiring, stream, io, cdc
from amaranth.lib.wiring import Out, In

from glasgow.support import logging
from glasgow.gateware.stream import AsyncQueue
from glasgow.gateware import pll
from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletError, GlasgowAppletV2


__all__ = ["TDMComponent", "TDMApplet"]

class TDMClockGen(wiring.Component):
    '''
    Generates bclk and fsync
    '''
    fsync: Out(1)
    bclk: Out(1)

    def __init__(self, bclk_period, sys_clk_period, frame_bytes):
        super().__init__()
        self._bclk_period = bclk_period
        self._sys_clk_period = sys_clk_period
        self._frame_bytes = frame_bytes

    def elaborate(self, platform):
        m = Module()

        # PLL seems tricky, lets see later
        # plan = pll.ClockPlan(self._sys_clk_period)
        # m.domains.bclk = cd_bclk = plan.add_domain(pll.Channel(period=self._bclk_period))
        # m.submodules.pll = plan.create(platform)
        # TODO min PLL frequency is 16MHz, need a divisor for lower frequencies

        # m.d.comb += self.bclk.eq(cd_bclk.clk)

        # fclk_counter = Signal(range(self._frame_bytes * 8))
        # with m.If(fclk_counter == self._frame_bytes * 8 - 1):
        #     m.d.comb += self.fsync.eq(1)
        #     m.d.bclk += fclk_counter.eq(0)
        # with m.Else():
        #     m.d.bclk += fclk_counter.eq(fclk_counter + 1)

        bclk_counter = Signal(range(64))
        m.d.sync += bclk_counter.eq(bclk_counter + 1)
        m.d.comb += self.bclk.eq(bclk_counter[-1])

        fclk_counter = Signal(range(self._frame_bytes * 8))

        with m.If(bclk_counter == 0):
            with m.If(fclk_counter == self._frame_bytes * 8 - 1):
                m.d.comb += self.fsync.eq(1)
                m.d.sync += fclk_counter.eq(0)
            with m.Else():
                m.d.sync += fclk_counter.eq(fclk_counter + 1)
        m.d.comb += self.fsync.eq(fclk_counter == self._frame_bytes * 8 - 1)


        return m

class TDMFrontend(wiring.Component):
    '''
    Shifts frame_bits bits in/out, keeps alignment with fsync.
    If fsync is received at an unexpected time, raises an error and discards data to keep correct alignment.
    '''
    bclk: In(1)
    fsync: In(1)
    rx: In(1)

    rx_stream: Out(stream.Signature(8))

    def __init__(self, ports, frame_bytes):
        self._ports = ports

        self._frame_bytes = frame_bytes 

        super().__init__()

    def elaborate(self, platform):
        m = Module()


        # Shifting logic works directly on incoming bclk clock
        m.domains.bclk = cd_bclk = ClockDomain(async_reset=True, local=True)
        m.d.comb += cd_bclk.clk.eq(self.bclk)

        m.submodules.fifo = fifo = AsyncQueue.shaped_like(self.rx_stream,
            depth=4, # CDC only, no buffering
            i_domain="bclk",
            o_domain="sync"
        )
        wiring.connect(m, wiring.flipped(self.rx_stream), fifo.o)

        fsync_rising = Signal(1)
        last_fsync = Signal(1)
        m.d.bclk += last_fsync.eq(self.fsync)
        m.d.comb += fsync_rising.eq(self.fsync & ~last_fsync)

        shift = Signal(1)
        shift_done = Signal(1)
        with m.If(shift):
            rx_shreg = Signal(8)
            m.d.bclk += rx_shreg.eq(Cat(self.rx, rx_shreg))
            m.d.comb += fifo.i.p.eq(Cat(self.rx, rx_shreg))

            # TODO Maybe these could be combined into one signal (use modulo 8 for when byte is full)
            bytes_shifted = Signal(range(self._frame_bytes))
            shift_count = Signal(range(8))
            with m.If(shift_count == 8 - 1):
                m.d.comb += fifo.i.valid.eq(1)
                m.d.bclk += shift_count.eq(0)
                with m.If(bytes_shifted == self._frame_bytes - 1):
                    m.d.bclk += bytes_shifted.eq(0)
                    m.d.comb += shift_done.eq(1)
                with m.Else():
                    m.d.bclk += bytes_shifted.eq(bytes_shifted + 1)

            with m.Else():
                m.d.bclk += shift_count.eq(shift_count + 1)

        with m.FSM(domain='bclk'):
            with m.State("Wait-fsync"):
                with m.If(fsync_rising):
                    # shift first bit immediately
                    m.d.comb += shift.eq(1)
                    m.next = "Shift-Frame"
                pass
            with m.State("Shift-Frame"):
                m.d.comb += shift.eq(1)
                with m.If(shift_done):
                    m.next = "Wait-fsync"
                # TODO detect wrong fsync timing
                pass
            with m.State("Fsync-early"):
                # Fsync received too early, send zeros and wait for next fsync
                # Once we have tx also, we will have to discard some tx bytes here
                # Have to be careful to keep rx/tx delay fixed ideally
                # TODO how do I best report this?
                # A flag in a register?
                # An extra byte with each frame with status bits?
                pass
            with m.State("Fsync-late"):
                # Fsync received late
                pass


        return m


# TODO maybe I don't need this wrapper
class TDMComponent(wiring.Component):
    o_stream: Out(stream.Signature(8))

    def __init__(self, ports, frame_bytes, bclk_generator_period, sys_clk_period):
        self._ports = ports
        self._frame_bytes = frame_bytes
        self._bclk_generator_period = bclk_generator_period
        self._sys_clk_period = sys_clk_period

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.frontend = frontend = TDMFrontend(self._ports, self._frame_bytes)
        wiring.connect(m, wiring.flipped(self.o_stream), frontend.rx_stream)

        if self._bclk_generator_period is not None:
            m.submodules.clockgen = clockgen = TDMClockGen(self._bclk_generator_period, self._sys_clk_period, self._frame_bytes)
            m.submodules.bclk_buffer   = bclk_buffer  = io.Buffer("o", self._ports.bclk)
            m.submodules.fsync_buffer  = fsync_buffer = io.Buffer("o", self._ports.fsync)
            m.d.comb += [
                bclk_buffer.o.eq(clockgen.bclk),
                fsync_buffer.o.eq(clockgen.fsync),
            ]
            m.d.comb += [
                frontend.bclk.eq(clockgen.bclk),
                frontend.fsync.eq(clockgen.fsync),
            ]
        else:
            m.submodules.bclk_buffer   = bclk_buffer  = io.Buffer("i", self._ports.bclk)
            m.submodules.fsync_buffer  = fsync_buffer = io.Buffer("i", self._ports.fsync)
            m.d.comb += [
                frontend.bclk.eq(bclk_buffer.i),
                frontend.fsync.eq(fsync_buffer.i),
            ]
            if platform is not None:
                # Works on iCE40HX8K.
                # TODO what does this do?
                platform.add_clock_constraint(bclk_buffer.i, 100e6)

        m.submodules.rx_buffer     = rx_buffer    = io.Buffer("i", self._ports.rx)
        m.d.comb += [
            frontend.rx.eq(rx_buffer.i),
        ]

        return m


class TDMInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
                 bclk: GlasgowPin, fsync: GlasgowPin, rx: GlasgowPin,
                 frame_bytes, bclk_generator_frequency: float | None):
        self._logger = logger
        self._level  = logging.DEBUG if self._logger.name == __name__ else logging.TRACE
        self._frame_bytes = frame_bytes

        ports = assembly.add_port_group(bclk=bclk, fsync=fsync, rx=rx)
        bclk_period = 1/bclk_generator_frequency if bclk_generator_frequency is not None else None
        component = assembly.add_submodule(TDMComponent(ports, frame_bytes, bclk_period, assembly.sys_clk_period))
        self._pipe = assembly.add_in_pipe(component.o_stream)

    def _log(self, message, *args):
        self._logger.log(self._level, "SPI analyzer: " + message, *args)

    async def read_frame(self):
        return (await self._pipe.recv(self._frame_bytes)).tobytes()

    # Maybe have a play/rec/playrec interface like the sounddevice library


class TDMApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "Generate or receive audio via TDM/I2S"
    description = """
    """
    # Not tested on anything else
    required_revision = "C0"

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)
        access.add_pins_argument(parser, "bclk",  required=True, default=True)
        access.add_pins_argument(parser, "fsync", required=True, default=True)
        access.add_pins_argument(parser, "rx",    required=True, default=True)
        # TODO implement tx later
        # access.add_pins_argument(parser, "tx",    required=True, default=True)
        # might support multiple data lines later
        #access.add_pins_argument(parser, "tx",    required=True, default=True, width=range(1, 8))

        # TODO maybe this could be a setup argument set by register
        # TODO might be easier to specify bitdepth, channels
        parser.add_argument(
            "--frame-bytes", type=int, default=4,
            help="bytes in a frame, bclk cycles per fsync divided by 8, e.g. 16 bit x 2 channels = 4 bytes")

        parser.add_argument(
            "--bclk-freq", metavar="FREQ", type=float,
            help="instead of using external clock, generate bit clock at FREQ Hz and derive fsync from it")

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.tdm_iface = TDMInterface(self.logger, self.assembly,
                bclk=args.bclk, fsync=args.fsync, rx=args.rx,
                frame_bytes=args.frame_bytes, bclk_generator_frequency=args.bclk_freq
            )

    @classmethod
    def add_run_arguments(cls, parser):
        # parser.add_argument(
        #     "-l", "--loop", action='store_true',
        #     help="loop the txfile until the command is aborted")

        # parser.add_argument(
        #     "--txfile", type=argparse.FileType('rb'),
        #     help="raw audio file to transmit")

        # parser.add_argument(
        #     "--rxfile", type=argparse.FileType('wb'),
        #     help="raw audio file to record to")
        pass

    async def run(self, args):
        for i in range(10):
            print(await self.tdm_iface.read_frame())
        # write_future = asyncio.create_task(write_task())
        # read_future = asyncio.create_task(read_task(write_future))
        # await asyncio.gather(write_future, read_future)

    @classmethod
    def tests(cls):
        from . import test
        return test.TDMAppletTestCase
