import logging
import asyncio
from amaranth import *
from amaranth.lib import io, fifo

from ... import *
from ....gateware.clockgen import ClockGen

class TDMSubtarget(Elaboratable):
    def __init__(self, ports, out_fifo, in_fifo, clock_dir, bclk_cyc, n_channels, n_bits, output_fsync_delay):
        self.ports = ports
        self.out_fifo = out_fifo
        self.in_fifo = in_fifo
        self.clock_dir = clock_dir
        self.bclk_cyc = bclk_cyc
        self.n_channels = n_channels
        self.n_bits = n_bits
        self.output_fsync_delay = output_fsync_delay

    def elaborate(self, platform):
        m = Module()

        bits_per_frame = self.n_channels * self.n_bits
        bytes_per_frame = self.n_channels * self.n_bits // 8

        m.domains.bclk = cd_bclk = ClockDomain("bclk")
        fsync = Signal()

        # TODO we might want to output fsync for more than one bclk
        # TODO if we receive fsync we might want to make sure it only stays on for one bclk

        # TODO allow disabling one of rx and tx

        # Generate or import bit clock
        if self.clock_dir == 'source':
            # TODO maybe better to use a PLL?
            m.submodules.clockgen = ClockGen(self.bclk_cyc)
            m.d.comb += cd_bclk.clk.eq(m.submodules.clockgen.clk)
            
            fsync_counter = Signal(range(bits_per_frame))
            m.d.bclk += fsync_counter.eq(fsync_counter + 1)
            m.d.comb += fsync.eq(fsync_counter == 0)

            m.submodules.bclk_buffer = bclk_buffer = io.Buffer("o", self.ports.bclk)
            m.submodules.fsync_buffer = fsync_buffer = io.Buffer("o", self.ports.fsync)
            m.d.comb += bclk_buffer.o.eq(m.submodules.clockgen.clk)
            m.d.comb += fsync_buffer.o.eq(fsync)
        else:
            assert self.clock_dir == 'sink'
            m.submodules.bclk_buffer = bclk_buffer = io.Buffer("i", self.ports.bclk)
            m.submodules.fsync_buffer = fsync_buffer = io.Buffer("i", self.ports.fsync)
            m.d.comb += cd_bclk.clk.eq(bclk_buffer.i)
        
        m.submodules.tx_buffer = tx_buffer = io.Buffer("o", self.ports.tx)
        m.submodules.rx_buffer = rx_buffer = io.Buffer("i", self.ports.rx)


        # do we actually need those or could we use self.out_fifo._fifo.r_level? self.out_fifo might not be async though

        # fifos for transfer between sync domain and bclk domain
        m.submodules.o_fifo = o_fifo = fifo.AsyncFIFO(width=8, depth=bytes_per_frame, r_domain='bclk', w_domain='sync')
        m.submodules.i_fifo = i_fifo = fifo.AsyncFIFO(width=8, depth=bytes_per_frame, r_domain='sync', w_domain='bclk')

        # Fill o_fifo from self.out_fifo
        with m.If(o_fifo.w_rdy & self.out_fifo.r_rdy):
            m.d.comb += o_fifo.w_en.eq(1)
            m.d.comb += self.out_fifo.r_en.eq(1)
            m.d.sync += o_fifo.w_data.eq(self.out_fifo.r_data)

        # Empty i_fifo into self.in_fifo
        with m.If(i_fifo.r_rdy & self.in_fifo.w_rdy):
            m.d.comb += i_fifo.r_en.eq(1)
            m.d.comb += self.in_fifo.w_en.eq(1)
            m.d.sync += self.in_fifo.w_data.eq(i_fifo.r_data)

        full_frame_in_fifo = Signal()
        m.d.comb += full_frame_in_fifo.eq(m.submodules.o_fifo.r_level >= bytes_per_frame - 1) # -1 because we keep 1 byte in the fifo out register, I think. Maybe we have to actually check if a byte is present there

        active_frame = Signal()
        with m.If(fsync):
            # Set the frame as active only if we have a full frame of data available in the fifo
            m.d.bclk += active_frame.eq(full_frame_in_fifo)
            # TODO maybe the inverse for i_fifo. Only activate if there is space for a full frame
            # TODO maybe we could count frame drops here and report them

        active_frame_imm = Signal()
        if self.output_fsync_delay == 0:
            m.d.comb += active_frame_imm.eq(Mux(fsync, full_frame_in_fifo, active_frame))
            # TODO active_frame_imm maybe also has to deassert one bclk earlier than active_frame
        elif self.output_fsync_delay == 1:
            m.d.comb += active_frame_imm.eq(active_frame)
        # TODO stop active_frame if the next fsync is not coming, i.e. count how many bits we already shifted out

        bits_valid_o = Signal(range(9))
        bits_valid_i = Signal(range(9))
        shreg_o = Signal(8)
        shreg_i = Signal(8)
        m.d.comb += tx_buffer.o.eq(shreg_o[-1] & active_frame_imm)
        
        with m.If(active_frame_imm):
            # Shift data in / out on every bclk
            m.d.bclk += shreg_o.eq(Cat(C(0, 1), shreg_o))
            m.d.bclk += shreg_i.eq(Cat(rx_buffer.i, shreg_i))
            m.d.bclk += bits_valid_o.eq(bits_valid_o - 1)
            m.d.bclk += bits_valid_i.eq(bits_valid_i + 1)
        
        with m.If(bits_valid_i >= 8):
            m.d.bclk += i_fifo.w_data.eq(shreg_i)
            m.d.bclk += i_fifo.w_en.eq(1)
            with m.If(active_frame_imm):  # If we also shifted in data in this cycle
                m.d.bclk += bits_valid_i.eq(1)
            with m.Else():
                m.d.bclk += bits_valid_i.eq(0)
        with m.Else():
            m.d.bclk += i_fifo.w_en.eq(0)
        
        with m.FSM(domain="bclk"):
            with m.State("Wait"):
                # Read a byte from the fifo as soon as it is available, it will be kept in r_data until we need it
                with m.If(o_fifo.r_rdy):
                    m.d.comb += o_fifo.r_en.eq(1)
                    m.next = "Read"
            with m.State("Read"):
                # When the shift register is empty refill it from r_data
                with m.If(bits_valid_o <= 1):
                    m.d.bclk += shreg_o.eq(o_fifo.r_data)
                    m.d.bclk += bits_valid_o.eq(8)

                    m.next = "Wait"


        return m

class TDMApplet(GlasgowApplet):
    logger = logging.getLogger(__name__)
    help = "Send and receive audio data via TDM/I2S"
    description = """
    """

    @classmethod
    def add_build_arguments(cls, parser, access):
        super().add_build_arguments(parser, access)

        access.add_pin_argument(parser, "rx", default=True)
        access.add_pin_argument(parser, "tx", default=True)
        access.add_pin_argument(parser, "bclk", default=True)
        access.add_pin_argument(parser, "fsync", default=True)

        parser.add_argument(
            "--clock-dir", metavar="DIR", choices=("source", "sink"),
            default="source",
            help="set clock direction as DIR (default: %(default)s)")
        parser.add_argument(
            "-r", "--sample-rate", metavar="FREQ", type=int, default=48000,
            help="set sample rate to FREQ Hz (default: %(default)s)")
        parser.add_argument(
            "-c", "--channels", type=int, default=2,
            help="number of channels (default: %(default)s)")
        parser.add_argument(
            "-d", "--bit-depth", type=int, default=16,
            help="bit depth, only multiples of 8 are supported (default: %(default)s)")
        parser.add_argument(
            "--fsync-delay", metavar="DEL", choices=(0, 1), default=0,
            help="output is valid DEL bclk cycles after fsync rising edge (default: %(default)s)")

    def build(self, target, args):
        assert args.bit_depth % 8 == 0
        bclk_frequency = args.sample_rate * args.channels * args.bit_depth
        bclk_cyc = self.derive_clock(input_hz=target.sys_clk_freq, output_hz=bclk_frequency)
        self.mux_interface = iface = target.multiplexer.claim_interface(self, args)
        return iface.add_subtarget(TDMSubtarget(
            ports=iface.get_port_group(
                rx=args.pin_rx,
                tx=args.pin_tx,
                bclk=args.pin_bclk,
                fsync=args.pin_fsync,
            ),
            out_fifo=iface.get_out_fifo(),
            in_fifo=iface.get_in_fifo(),
            clock_dir=args.clock_dir,
            bclk_cyc=bclk_cyc,
            n_channels=args.channels,
            n_bits=args.bit_depth,
            output_fsync_delay=args.fsync_delay
            ))
        

    @classmethod
    def add_run_arguments(cls, parser, access):
        super().add_run_arguments(parser, access)

    async def run(self, device, args):
        iface = await device.demultiplexer.claim_interface(self, self.mux_interface, args)
        return iface

    async def interact(self, device, args, iface):
        test_data = [0xFF, 0x00, 0x01, 0x02] * 2
        print(test_data)
        await iface.write(test_data)
        print(bytes(await iface.read(len(test_data))))
        await iface.write(test_data)
        print(bytes(await iface.read(len(test_data))))

    @classmethod
    def tests(cls):
        from . import test
        return test.TDMAppletTestCase