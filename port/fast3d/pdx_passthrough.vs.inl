// cgc version 3.1.0013, build date Apr 18 2012
// command line args: -profile vp20
// source file: port\fast3d\pdx_passthrough.vs.cg
//vendor NVIDIA Corporation
//version 3.1.0.13
//profile vp20
//program main
//semantic main.viewport
//semantic main.noiseTransform
//var float4 input.position : $vin.POSITION : ATTR0 : 0 : 1
//var float4 input.color : $vin.DIFFUSE : ATTR3 : 0 : 1
//var float4 input.specular : $vin.SPECULAR : ATTR4 : 0 : 1
//var float2 input.tex0 : $vin.TEXCOORD0 : TEXCOORD0 : 0 : 1
//var float2 input.tex1 : $vin.TEXCOORD1 : TEXCOORD1 : 0 : 1
//var float4x4 viewport :  : c[0], 4 : 1 : 1
//var float4 noiseTransform :  : c[4] : 2 : 1
//var float4 main.position : $vout.POSITION : HPOS : -1 : 1
//var float4 main.color : $vout.COLOR0 : COL0 : -1 : 1
//var float4 main.specular : $vout.COLOR1 : COL1 : -1 : 1
//var float4 main.tex0 : $vout.TEXCOORD0 : TEX0 : -1 : 1
//var float4 main.tex1 : $vout.TEXCOORD1 : TEX1 : -1 : 1
//var float4 main.tex2 : $vout.TEXCOORD2 : TEX2 : -1 : 1
//const c[5] = 0 1
// 18 instructions, 0 R-regs
0x00000000, 0x0400001b, 0x08361300, 0x200807f8,
0x00000000, 0x0040001b, 0x0800086c, 0x2e0007f8,
0x00000000, 0x004c2055, 0x0436186c, 0x2e1007f8,
0x00000000, 0x008c0000, 0x0436186c, 0x5e1007f8,
0x00000000, 0x008c40aa, 0x0436186c, 0x5e0007f8,
0x00000000, 0x006c601b, 0x0436106c, 0x3e0007f8,
0x00000000, 0x0020001b, 0x0836106c, 0x210007f8,
0x00000000, 0x008c801b, 0x04361aec, 0x3c1007f8,
0x00000000, 0x0020001b, 0x0436106c, 0x2070f800,
0x00000000, 0x004000ff, 0x0836286c, 0x2070c858,
0x00000000, 0x0020061b, 0x0836106c, 0x2070f818,
0x00000000, 0x0020081b, 0x0836106c, 0x2070f820,
0x00000000, 0x0020121b, 0x0836106c, 0x2070c848,
0x00000000, 0x002ca011, 0x0c36106c, 0x20703848,
0x00000000, 0x0020141b, 0x0836106c, 0x2070c850,
0x00000000, 0x002ca011, 0x0c36106c, 0x20703850,
0x00000000, 0x002ca000, 0x0c36106c, 0x20702858,
0x00000000, 0x0020001b, 0x0836106c, 0x20701859,
