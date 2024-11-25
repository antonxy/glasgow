from amaranth import *
from amaranth.sim import Simulator
import unittest
import argparse
import functools
import types
import asyncio
import threading

from ... import *
from . import TDMApplet
from ....access.simulation.arguments import SimulationArguments

from ....access.simulation import *
from ....target.simulation import *
from ....device.simulation import *
from glasgow.target.hardware import GlasgowHardwareTarget
from glasgow.device.hardware import GlasgowHardwareDevice
from glasgow.access.direct import DirectMultiplexer, DirectDemultiplexer, DirectArguments



def multi_applet_simulation_test(setup):
    def decorator(case):
        @functools.wraps(case)
        def wrapper(self):

            getattr(self, setup)()
            @types.coroutine
            def run():
                yield from case(self)

            sim = Simulator(self.target)
            sim.add_clock(1e-9)
            sim.add_sync_process(run)
            vcd_name = f"{case.__name__}.vcd"
            with sim.write_vcd(vcd_name):
                sim.run()
            print(vcd_name)
            # os.remove(vcd_name)

        return wrapper

    return decorator


def multi_applet_hardware_test():
    def decorator(case):
        @functools.wraps(case)
        def wrapper(self):
            exception = None
            def run_test():
                try:
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(case(self))

                except Exception as e:
                    nonlocal exception
                    exception = e

                finally:
                    if self.device is not None:
                        loop.run_until_complete(self.device.demultiplexer.cancel())
                    loop.close()

            thread = threading.Thread(target=run_test)
            thread.start()
            thread.join()
            if exception is not None:
                raise exception

        return wrapper

    return decorator


class MultiAppletTestCase(unittest.TestCase):
    def _prepare_simulation_target(self):
        self.target = GlasgowSimulationTarget(multiplexer_cls=SimulationMultiplexer)

        self.device = GlasgowSimulationDevice(self.target)
        self.device.demultiplexer = SimulationDemultiplexer(self.device)

    def _parse_applet_args(self, applet, args, interact=False):
        access_args = SimulationArguments(applet)
        parser = argparse.ArgumentParser()
        applet.add_build_arguments(parser, access_args)
        applet.add_run_arguments(parser, access_args)
        if interact:
            applet.add_interact_arguments(parser)
        return parser.parse_args(args)


class TDMAppletTestCase(GlasgowAppletTestCase, applet=TDMApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds(args=['--pin-bclk', "0", '--pin-fsync', "1", '--pin-rx', "2", '--pin-tx', "3"])

    def setup_loopback(self):
        self.build_simulated_applet()
        mux_iface = self.applet.mux_interface
        ports = mux_iface._subtargets[0].ports
        m = Module()
        m.d.comb += ports.rx.i.eq(ports.tx.o)
        self.target.add_submodule(m)
    
    async def check_loopback(self, frame_size=4):
        tdm_iface = await self.run_simulated_applet()

        test_data = bytes([i for i in range(frame_size)])

        await tdm_iface.write(test_data)
        result = await tdm_iface.read(len(test_data))
        self.assertEqual(result, test_data)

        await tdm_iface.write(test_data)
        result = await tdm_iface.read(len(test_data))
        self.assertEqual(result, test_data)

    @applet_simulation_test("setup_loopback", ['--pin-bclk', "0", '--pin-fsync', "1", '--pin-rx', "2", '--pin-tx', "3"])
    async def test_loopback(self):
        await self.check_loopback()
    
    @applet_simulation_test("setup_loopback", ['--pin-bclk', "0", '--pin-fsync', "1", '--pin-rx', "2", '--pin-tx', "3", '--fsync-delay', '1'])
    async def test_loopback2(self):
        await self.check_loopback()

    @applet_simulation_test("setup_loopback", ['--pin-bclk', "0", '--pin-fsync', "1", '--pin-rx', "2", '--pin-tx', "3", '--fsync-duration', '6'])
    async def test_loopback3(self):
        await self.check_loopback()

    @applet_simulation_test("setup_loopback", ['--pin-bclk', "0", '--pin-fsync', "1", '--pin-rx', "2", '--pin-tx', "3", '--extra-cycles', '6'])
    async def test_loopback4(self):
        await self.check_loopback()

    @applet_simulation_test("setup_loopback", ['--pin-bclk', "0", '--pin-fsync', "1", '--pin-rx', "2", '--pin-tx', "3", '--channels', '3', '--bit-depth', '24'])
    async def test_loopback4(self):
        await self.check_loopback(frame_size=9)

    
    # TODO test buffer underflow
    # TODO test with recorded traces. Problem: looks like there is no vcd reader available in the Simulator

# This is currently not loaded by `glasgow test` but can be run using `python -m unittest`
class TDMMultiAppletTestCase(MultiAppletTestCase):

    def setup_source_sink(self):
        self._prepare_simulation_target()

        args_source = ['--pin-bclk', "0", '--pin-fsync', "1", '--pin-tx', "2", '--clock-dir', 'source', '--fsync-duration', "5", '--extra-cycles', '3']
        args_sink = ['--pin-bclk', "4", '--pin-fsync', "5", '--pin-rx', "6", '--clock-dir', 'sink']

        self.source_applet = TDMApplet()
        self.source_parsed_args = self._parse_applet_args(self.source_applet, args_source)
        subtarget_source = self.source_applet.build(self.target, self.source_parsed_args, name='source_applet')

        self.sink_applet = TDMApplet()
        self.sink_parsed_args = self._parse_applet_args(self.sink_applet, args_sink)
        subtarget_sink = self.sink_applet.build(self.target, self.sink_parsed_args, name='sink_applet')

        source_ports = subtarget_source.ports
        sink_ports = subtarget_sink.ports

        m = Module()
        m.d.comb += sink_ports.rx.i.eq(source_ports.tx.o)
        m.d.comb += sink_ports.bclk.i.eq(source_ports.bclk.o)
        m.d.comb += sink_ports.fsync.i.eq(source_ports.fsync.o)
        self.target.add_submodule(m)

    @multi_applet_simulation_test("setup_source_sink")
    async def test_source_sink(self):
        source_iface = await self.source_applet.run(self.device, self.source_parsed_args)
        sink_iface = await self.sink_applet.run(self.device, self.sink_parsed_args)

        empty_frame = b"\x00" * 4
        test_data = b"\xFF\x00\x01\x02" * 2

        await source_iface.write(test_data)

        # The first frame is read as zeros because the source needs a moment to get going
        result = await sink_iface.read(len(empty_frame + test_data))
        self.assertEqual(result, empty_frame + test_data)


    @multi_applet_hardware_test()
    async def test_hardware_multi_applet(self):
        device = GlasgowHardwareDevice()
        self.device = device
        await device.reset_alert("AB")
        await device.poll_alert()
        await device.set_voltage("AB", 3.3)
        target = GlasgowHardwareTarget(revision=device.revision,
                                        multiplexer_cls=DirectMultiplexer,
                                        with_analyzer=False)
        access_args = DirectArguments(applet_name="tdm",
                                        default_port="AB",
                                        pin_count=16)
        tdm0_parser = argparse.ArgumentParser('tdm0')
        tdm1_parser = argparse.ArgumentParser('tdm1')
        TDMApplet.add_build_arguments(tdm0_parser, access_args)
        TDMApplet.add_build_arguments(tdm1_parser, access_args)
        TDMApplet.add_run_arguments(tdm0_parser, access_args)
        TDMApplet.add_run_arguments(tdm1_parser, access_args)
        TDMApplet.add_interact_arguments(tdm0_parser)
        TDMApplet.add_interact_arguments(tdm1_parser)

        # Assumming ping 0,1,2 are connected to 8,9,10 respectively
        tdm0_args = tdm0_parser.parse_args(["-V", "3.3", '--pin-bclk', "0", '--pin-fsync', "1", '--pin-tx', "2", '--clock-dir', 'source'])
        tdm1_args = tdm0_parser.parse_args(["-V", "3.3", '--pin-bclk', "8", '--pin-fsync', "9", '--pin-rx', "10", '--clock-dir', 'sink'])
        tdm0 = TDMApplet()
        tdm1 = TDMApplet()
        tdm0.build(target, tdm0_args)
        tdm1.build(target, tdm1_args)
        plan = target.build_plan()
        await device.download_target(plan)
        device.demultiplexer = DirectDemultiplexer(device, target.multiplexer.pipe_count)

        iface1 = await tdm1.run(device, tdm1_args) # Start receiver first so that it's ready before sender starts sending
        iface0 = await tdm0.run(device, tdm0_args)
        await iface1.read() # Clear the receiver from previous runs

        test_data = b'abcdabceabcfabcg' * 100

        await iface0.write(test_data)
        await iface0.flush(wait=False)

        # TODO a bunch of zeros are received before the actual data.
        # The clock source should maybe wait to enable clock until it's fifo is somewhat filled
        result = bytes(await iface1.read(len(test_data) + 500))
        self.assertIn(test_data, result)