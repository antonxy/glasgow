# Reference: https://ww1.microchip.com/downloads/en/DeviceDoc/41342E.pdf
# Accession: TODO

import os
import math
import asyncio
import logging
import argparse
import struct
from amaranth import *
from amaranth.lib import enum, data, wiring, stream, io
from amaranth.lib.wiring import In, Out

from ... import *
from ....support.logging import dump_hex
from glasgow.applet.control.gpio import GPIOInterface
from glasgow.abstract import AbstractAssembly, GlasgowPin, ClockDivisor
from glasgow.applet import GlasgowAppletError, GlasgowAppletV2

# Command structure in fifo:
# byte 1: internal command, pic 4 bit command
# byte 2: pic 4 bit command
# byte 3,4: pic 16bit payload
# combine byte 1 and 2? Or is it easier if they are separate?

# internal commands:
# Read, Write, Delay

# For every command in the in fifo, 2 bytes are sent in the out fifo
# 

CMD_READ      = 0b0000
CMD_WRITE     = 0b0001
CMD_DELAY     = 0b0010


class DataShifter(wiring.Component):
    cmd_valid: In(1) # Indicates all In fields have been set by the outer component. They must be kept valid until cmd_done is set.
    cmd_done: Out(1) # Indicates the command has been sent on the bus and the Out fields are set by this component

    cmd_pic: In(4)
    payload_write: In(16)
    payload_read: Out(16)
    payload_oe: In(1)

    divisor: In(16)

    def __init__(self, ports):
        self._ports = ports

        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules['buffer_pgc'] = buffer_pgc = io.Buffer("o", self._ports.pgc)
        m.submodules['buffer_pgd'] = buffer_pgd = io.Buffer("io", self._ports.pgd)

        ### Clock
        clock_cycles = Signal(5)
        timer = Signal.like(self.divisor)
        clock = Signal(1)
        m.d.comb += buffer_pgc.oe.eq(1)
        m.d.comb += clock.eq((timer * 2 <= self.divisor) & (clock_cycles > 0))
        m.d.comb += buffer_pgc.o[0].eq(clock)

        with m.If(clock_cycles > 0):
            with m.If(timer == self.divisor):
                m.d.sync += timer.eq(0)
                m.d.sync += clock_cycles.eq(clock_cycles - 1)
            with m.Else():
                m.d.sync += timer.eq(timer + 1)
        with m.Else():
            m.d.sync += timer.eq(0)

        last_clock = Signal(1)
        m.d.sync += last_clock.eq(clock)

        rising = Signal(1)
        m.d.comb += rising.eq(~last_clock & clock)

        falling = Signal(1)
        m.d.comb += falling.eq(last_clock & ~clock)

        ### Shifting
        shreg_o = Signal(16)
        shreg_i = Signal(16)

        with m.If(rising): # shift out
            m.d.sync += buffer_pgd.o[0].eq(shreg_o[0])
            m.d.sync += shreg_o.eq(Cat(shreg_o[1:], C(0, 1)))

        with m.If(falling): # shift in 
            m.d.sync += shreg_i.eq(Cat(shreg_i[1:], buffer_pgd.i[0]))

        # TODO pgd o should maybe go low in pause


        ### State machine
        pause_timer = Signal.like(self.divisor)

        with m.FSM() as fsm:
            with m.State("IDLE"):
                m.d.comb += buffer_pgd.oe.eq(0)
                with m.If(self.cmd_valid):
                    m.next = "SEND-CMD"
                    m.d.sync += clock_cycles.eq(4)
                    m.d.sync += shreg_o[0:4].eq(self.cmd_pic)
            with m.State("SEND-CMD"):
                m.d.comb += buffer_pgd.oe.eq(1)
                with m.If(clock_cycles == 0):
                    m.next = "PAUSE"
                    m.d.sync += pause_timer.eq(0)
            with m.State("PAUSE"):
                m.d.sync += pause_timer.eq(pause_timer + 1)
                with m.If(pause_timer == self.divisor):
                    m.next = "SEND-PAYLOAD"
                    m.d.sync += clock_cycles.eq(16)
                    m.d.sync += shreg_o.eq(self.payload_write)
            with m.State("SEND-PAYLOAD"):
                m.d.comb += buffer_pgd.oe.eq(self.payload_oe)
                with m.If(clock_cycles == 0):
                    m.d.sync += self.payload_read.eq(shreg_i)
                    m.next = "DONE"
            with m.State("DONE"):
                m.d.comb += buffer_pgd.oe.eq(0)
                m.d.comb += self.cmd_done.eq(1)
                m.next = "IDLE"

        return m


class ProgramPIC18fComponent(wiring.Component):
    i_stream: In(stream.Signature(8))
    o_stream: Out(stream.Signature(8))

    divisor: In(16)

    def __init__(self, ports):
        self._ports = ports

        super().__init__()

    def elaborate(self, platform):
        m = Module()


        # Components we need:
        # - clock generator (active when shifting in/out data)
        # - shift registers
        # - state machine reading / writing fifo
        # - state machine managing shifting of data and output enable

        # Maybe clock, shift registers, and shreg management should go into a sub component

        m.submodules.shifter = shifter = DataShifter(ports=self._ports)
        m.d.comb += shifter.divisor.eq(self.divisor)

        ### FIFO
        cmd_internal = Signal(4)

        with m.FSM() as fsm:
            with m.State("RECV-COMMAND"):
                m.d.comb += self.i_stream.ready.eq(1)
                with m.If(self.i_stream.valid):
                    m.d.sync += cmd_internal.eq(self.i_stream.payload[4:8])
                    m.d.sync += shifter.cmd_pic.eq(self.i_stream.payload[0:4])
                    m.next = "RECV-PAYLOAD1"
            with m.State("RECV-PAYLOAD1"):
                m.d.comb += self.i_stream.ready.eq(1)
                with m.If(self.i_stream.valid):
                    m.d.sync += shifter.payload_write[0:8].eq(self.i_stream.payload)
                    m.next = "RECV-PAYLOAD2"
            with m.State("RECV-PAYLOAD2"):
                m.d.comb += self.i_stream.ready.eq(1)
                with m.If(self.i_stream.valid):
                    m.d.sync += shifter.payload_write[8:16].eq(self.i_stream.payload)
                    m.next = "EXEC"
            with m.State("EXEC"):
                m.d.comb += shifter.payload_oe.eq(cmd_internal[0])
                m.d.comb += shifter.cmd_valid.eq(1)
                with m.If(shifter.cmd_done):
                    m.next = "SEND-PAYLOAD1"
            with m.State("SEND-PAYLOAD1"):
                m.d.comb += [
                    self.o_stream.payload.eq(shifter.payload_read[0:8]),
                    self.o_stream.valid.eq(1)
                ]
                with m.If(self.o_stream.ready):
                    m.next = "SEND-PAYLOAD2"
            with m.State("SEND-PAYLOAD2"):
                m.d.comb += [
                    self.o_stream.payload.eq(shifter.payload_read[8:16]),
                    self.o_stream.valid.eq(1)
                ]
                with m.If(self.o_stream.ready):
                    m.next = "RECV-COMMAND"

        return m

class ProgramPIC18fInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *, pgd: GlasgowPin, pgc: GlasgowPin, pgm: GlasgowPin, mclr: GlasgowPin):
        self._logger = logger
        self._level  = logging.DEBUG if self._logger.name == __name__ else logging.TRACE
        self._pgm_iface = GPIOInterface(logger, assembly, pins=(pgm,))
        self._mclr_iface = GPIOInterface(logger, assembly, pins=(mclr,))

        # assembly.use_pulls({pgc: "high", pgd: "high"})
        ports = assembly.add_port_group(pgd=pgd, pgc=pgc)
        component = assembly.add_submodule(ProgramPIC18fComponent(ports))
        self._pipe = assembly.add_inout_pipe(component.o_stream, component.i_stream)
        self._clock = assembly.add_clock_divisor(component.divisor,
            ref_period=assembly.sys_clk_period * 4, name="pgc")

    def _log(self, message, *args):
        self._logger.log(self._level, "PIC18f: " + message, *args)

    async def enter_low_voltage_program_mode(self):
        await self._pgm_iface.output(0, True)
        # TODO Does such a short sleep even make sense?
        # TODO asyncio sleep is not allowed in tests, how do I make a sleep that works on real hardware and in tests?
        # Could use delay of PIC Component, that will also make sure GPIOs are synchronized with the rest, if that's not anyway the case
        # await asyncio.sleep(2e-6) # P15 = 2us
        await self._mclr_iface.output(0, True)
        # await asyncio.sleep(2e-6) # P12 = 2us

    async def exit_low_voltage_program_mode(self):
        # No sleep required, P16 = 0s
        await self._mclr_iface.output(0, True)
        # No sleep required, P18 = 0s
        await self._pgm_iface.output(0, True)

    async def delay(self, delay):
        cmd = CMD_DELAY << 4
        payload = delay
        await self._pipe.send(struct.pack("<BH", cmd, payload))
        await self._pipe.flush()
        _ = await self._pipe.recv(2)

    # TODO offer an API where a bunch of reads/ writes can be queued up without flushing inbetween all the time
    async def read(self, cmd_4bit):
        cmd = CMD_READ << 4 | cmd_4bit & 0xf
        payload = 0xabcd
        await self._pipe.send(struct.pack("<BH", cmd, payload))
        await self._pipe.flush()
        octets = await self._pipe.recv(2)
        return struct.unpack("<H", octets)[0]

    async def write(self, cmd_4bit, payload):
        cmd = CMD_WRITE << 4 | cmd_4bit & 0xf
        await self._pipe.send(struct.pack("<BH", cmd, payload))
        await self._pipe.flush()
        _ = await self._pipe.recv(2)

    async def write_readback(self, cmd_4bit, payload):
        cmd = CMD_WRITE << 4 | cmd_4bit & 0xf
        await self._pipe.send(struct.pack("<BH", cmd, payload))
        await self._pipe.flush()
        readback = await self._pipe.recv(2)
        return struct.unpack("<H", readback)[0]

    async def set_tblptr(self, addr):
        addr_h = (addr >> 16) & 0xff
        addr_m = (addr >> 8) & 0xff
        addr_l = (addr >> 0) & 0xff
        await self.write(0000, 0x0E << 8 | addr_h)
        await self.write(0000, 0x6EF8)
        await self.write(0000, 0x0E << 8 | addr_m)
        await self.write(0000, 0x6EF7)
        await self.write(0000, 0x0E << 8 | addr_l)
        await self.write(0000, 0x6EF6)

    async def read_device_id(self):
        # Set TBLPTR = 0x3ffffe
        await self.set_tblptr(0x3ffffe)
        # Read low byte with post increment
        devid1 = await self.read(1001) >> 8
        # Read high byte with post increment
        devid2 = await self.read(1001) >> 8
        return devid1, devid2

pic18f_device_ids = {
    0b000: "PIC18LF13K50",
    0b001: "PIC18LF14K50",
    0b010: "PIC18F13K50",
    0b011: "PIC18F14K50",
}

class ProgramPIC18fApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "program PIC18f microcontrollers"
    description = """
    """

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)

        # Order matches the pin order, in clockwise direction.
        access.add_pins_argument(parser, "pgd",  default=True, required=True)
        access.add_pins_argument(parser, "pgc",  default=True, required=True)
        access.add_pins_argument(parser, "pgm",  default=True, required=True)
        access.add_pins_argument(parser, "mclr", default=True, required=True)

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.pic_iface = ProgramPIC18fInterface(self.logger, self.assembly, pgd=args.pgd, pgc=args.pgc, pgm=args.pgm, mclr=args.mclr)

    @classmethod
    def add_run_arguments(cls, parser):
        p_operation = parser.add_subparsers(dest="operation", metavar="OPERATION", required=True)

        p_device_id = p_operation.add_parser(
            "device-id", help="Read device id")

        p_program = p_operation.add_parser(
            "program", help="program MCU memory contents")
        p_program.add_argument(
            "file", metavar="HEX-FILE", type=argparse.FileType("rb"),
            help="firmware file to read (in Intel HEX format)")

    async def run(self, args):
        try:
            await self.pic_iface._clock.set_frequency(1e6)
            await self.pic_iface.enter_low_voltage_program_mode()

            if args.operation == "device-id":
                devid1, devid2 = await self.pic_iface.read_device_id()
                self.logger.info(f"DEVID1: %#02x, DEVID2: %#02x", devid1, devid2)
                if devid2 != 0x47:
                    self.logger.error("Failed to read device id")
                else:
                    try:
                        self.logger.info(f"Device: %s", pic18f_device_ids[devid1 >> 5])
                    except KeyError:
                        self.logger.error("Unknown device")

            if args.operation == "program":
                pass


        finally:
            await self.pic_iface.exit_low_voltage_program_mode()

    @classmethod
    def tests(cls):
        from . import test
        return test.ProgramPIC18fAppletTestCase
