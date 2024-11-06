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

        test_data = [0xFF, 0x00, 0x01, 0x02] * 2
        print(test_data)
        await tdm_iface.write(test_data)
        print(await tdm_iface.read(len(test_data))) # TODO why is there some extra byte in front? I don't see that being written
        await tdm_iface.write(test_data)
        print(await tdm_iface.read(len(test_data)))

        # TODO test buffer underflow
