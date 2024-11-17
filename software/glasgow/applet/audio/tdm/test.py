from amaranth import *
from amaranth.sim import Simulator
import unittest
import argparse
import functools
import types

from ... import *
from . import TDMApplet
from ....access.simulation.arguments import SimulationArguments

from ....access.simulation import *
from ....target.simulation import *
from ....device.simulation import *



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

    @applet_simulation_test("setup_loopback", ['--pin-bclk', "0", '--pin-fsync', "1", '--pin-rx', "2", '--pin-tx', "3"])
    async def test_loopback(self):
        tdm_iface = await self.run_simulated_applet()

        test_data = b"\xFF\x00\x01\x02" * 2

        await tdm_iface.write(test_data)
        result = await tdm_iface.read(len(test_data))
        self.assertEqual(result, test_data)

        await tdm_iface.write(test_data)
        result = await tdm_iface.read(len(test_data))
        self.assertEqual(result, test_data)

        # TODO test buffer underflow

# This is currently not loaded by `glasgow test` but can be run using `python -m unittest`
class TDMMultiAppletTestCase(MultiAppletTestCase):

    def setup_source_sink(self):
        self._prepare_simulation_target()

        args_source = ['--pin-bclk', "0", '--pin-fsync', "1", '--pin-tx', "2", '--clock-dir', 'source', '--fsync-duration', "5"]
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