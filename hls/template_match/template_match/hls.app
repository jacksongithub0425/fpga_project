<AutoPilot:project xmlns:AutoPilot="com.autoesl.autopilot.project" top="tme_top" name="template_match" ideType="classic">
    <files>
        <file name="norm_rsqrt.cpp" sc="0" tb="false" cflags="" csimflags="" blackbox="false"/>
        <file name="correlation_core.cpp" sc="0" tb="false" cflags="" csimflags="" blackbox="false"/>
        <file name="tme_top.h" sc="0" tb="false" cflags="" csimflags="" blackbox="false"/>
        <file name="tme_top.cpp" sc="0" tb="false" cflags="" csimflags="" blackbox="false"/>
        <file name="../../tb_patch.bin" sc="0" tb="1" cflags="-Wno-unknown-pragmas" csimflags="" blackbox="false"/>
        <file name="../../tb_templ.bin" sc="0" tb="1" cflags="-Wno-unknown-pragmas" csimflags="" blackbox="false"/>
        <file name="../../tb_golden.txt" sc="0" tb="1" cflags="-Wno-unknown-pragmas" csimflags="" blackbox="false"/>
        <file name="../../tme_tb.cpp" sc="0" tb="1" cflags="-Wno-unknown-pragmas" csimflags="" blackbox="false"/>
    </files>
    <solutions>
        <solution name="solution1" status=""/>
    </solutions>
    <Simulation argv="">
        <SimFlow name="csim" setup="false" optimizeCompile="false" clean="false" ldflags="" mflags=""/>
    </Simulation>
</AutoPilot:project>
