import types
from amaranth import *
from amaranth.lib import io

from ... import *
from . import TDMApplet


class TDMAppletTestCase(GlasgowAppletTestCase, applet=TDMApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds(args=[])

    def setup_loopback(self):
        self.build_simulated_applet()
        mux_iface = self.applet.mux_interface
        ports = mux_iface._subtargets[0].ports
        m = Module()
        m.d.comb += ports.rx.i.eq(ports.tx.o)
        self.target.add_submodule(m)

    @applet_simulation_test("setup_loopback", [])
    async def test_loopback(self):
        mux_iface = self.applet.mux_interface
        tdm_iface = await self.run_simulated_applet()

        test_data = b"\xFF\x00\x01\x02" * 2

        await tdm_iface.write(test_data)
        result = await tdm_iface.read(len(test_data))
        self.assertEqual(result, test_data)

        await tdm_iface.write(test_data)
        result = await tdm_iface.read(len(test_data))
        self.assertEqual(result, test_data)

        # TODO test buffer underflow
