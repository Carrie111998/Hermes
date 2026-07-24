#!/usr/bin/env python3
"""Standalone Atlas Opus compatibility probe.

This single file uses only the Python standard library. It sends one direct
Atlas request per model/stage pair, never retries, and never calls Hermes or
Run Orchestrator, so no model fallback can occur.

Stages:
  direct_stream       Minimal streaming request without tools.
  minimal_tools       Six dotted tool names with tiny schemas.
  full_agent_context  Embedded snapshot of the real Ultra Studio system prompt
                      and six complete Run Orchestrator tool schemas.

The output separates the client-generated correlation ID from Atlas'
x-request-id. If Atlas disconnects before HTTP response headers, the latter is
unavailable but the client correlation ID is still retained.
"""

from __future__ import annotations

import argparse
import base64
import bz2
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MODELS = (
    "anthropic/claude-opus-4.6",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-opus-4.8",
)
TOOL_NAMES = (
    "ask_user_question",
    "platform.prompt_compile",
    "media.model_catalog",
    "media.estimate_cost",
    "media.generate_image",
    "media.generate_video",
)
DEFAULT_STAGES = ("direct_stream", "minimal_tools")
FULL_STAGE = "full_agent_context"
ALL_STAGES = (*DEFAULT_STAGES, FULL_STAGE)
EXPECTED_CONTENT = "MODEL_TEST_OK"
PROFILE_SHA256 = "dc4db941ab622e7b06fd93aaa8678c5b81cc4fb69225a0149c026e3752044dc3"
PROFILE_B85 = """
LRx4!F+o`-Q(5-0DeVA7R^Q=K0Db@e{lD-3|NsC0|3Cl)01yCRIhxA)GKEn<0U!@;A}Ro>3Zvt-@?7VGd2!Uc+s?fm_TKK#w(jk0
YWDW?y=9*6=;uIds`KCnruS!9>W=%l%e&8SeQ&PW+TglXcXyrMRp@AJ89DCmJnciWn%6dKjIO20t*zB(K_Oz<+4sp5r+U78ro8$9
?%p>$<Gt@afbVOr=X&+aVX5d42quIAYI<lC*(miMqtqUt10ZRj(X})HX^<d-6w0UN9;TDj0g;3m4F-S!0MO9T5(yDNnlzhJ)X16{
292c9000d%G&BGJNu-d3(t3?Dr=>QYnx-c7q3UGXJtwI>OaKB5fHVyRNeBcAV53c{Z&f|DOjFZS$p#VXHi!Vw1Jnc51e%aQniEq^
Pa#bnsp>QvNHThWXaE2J2dF>N{O80!jnCuoKjnn}`;FoHxZVx6-gqQJW?m;1i!pT~h|T%q^su3jlbK3C)5hcy4|~d_bU!0%pP+xo
;M8Y1%j{4-DaZ%R3OpG$Y0`V@CH{TdfbS#!b(r31oPZ7rZa`1_Y%m|4*mg8E#{GHZTYrsI07;e^7X)(rRymC#Tgx^5QvRm`N<ajY
zGGYQVek0}<CGAc5w2uVghBIZ!rUxNmg;TnLOMc<T%SJ~dFhJ{=?XUC$^JICeERX}%Jp%ioD=dx{T+~^i4r+qv#*~X&pahC;B--M
Rqf<savfop;!4nobD4Q{6PI{1m$*M&{Li;<)^_J_j3uQClm|MV<PJF@VsPxeqG9!?9M*lOm}=d2rU4=4Jap3bXi4dDhau&2M|J&B
)cqzKQ%p&`>9J}MZ%T@C=QtFd9b0W2EGo@r`46_s$f`zy$wJo1nR$s*7a`pwxdqE^*Q(WOtKqa29YrXaG*6o1tg%YbT2OzCu?tkv
bW?9_7)PD<rBy@l*e=(9Tg)*PAoI2=i6ppox=+*1MW&M2#sWUYLV+s^-D(R!qVg+JlhTWj;_k}b6+zb(7sIHu(GvcYsH7}h1PW=`
lR9@{#*^{2Wm_QP3E~s@rL1Qg?K>SS6Qb$n-!CF6Ze7*B-8BzYcPuch6(r`Jpn%PF7TS7Ejbz*vJGO@tE_`j)d3);dQP*RsiK+W)
bNFY}4~`x$NtO>Juv?YK4XX1*IWT1<-#vVxfJ)tJ1{fdq!s2FT;K-9^!UnaSbVg$+V_)Q8wT>Dtm%?<&&%TzdtK!&kJyjs|%Dob%
A<)w`Cs<I?DcGo{R@J0N{r72N$g=MDP_Y2~P=MtMCQ?2|I^kmI;*xd;thD^Kqxi3NXQEdKLrGB*t5*3H%qc<$ASo8hfbCKOKuRj8
W@L8aiy3~5mc^||#wysbfa0j}!mBrCt8=cgeK>(?af@gf#Vu~rd7u?D2_5}>UM(w;AuzJ8krc>v==f7aeR6}+3p$LMte_l@p4Nm8
nmM5}#Np>&*xQw{@v7!_z0!p(NE#;YCrBlH8DzqaTiWk$GG?qnB`0(ULaYS}{(}vcfW3H1Sp%v`460O+Gqu`$sVR$HJnDeDNtTSZ
>d<VGM^39TGz8$1d2w0toJ{JnuDa%-(wSFkGBT`Gw9{L)#=%^7(u2h*j(u^gZ=)~1;e3ck?l@V6R|Z@=`2LPVG=nU#<Y?BqBEdm7
vn&@gA!n~uB52S%dPh2o2z5h7S7F%^cJk4@ylc(oMZV|VnM0*hb%Bj+#x$P1=TYL@=~^4fOHGt%B#jRsWltia1<^bz>pEJ|#RiYE
d!K7!$!YO`(s3M)F?TF&M3_GE>`16x{2}iOwB({xL=px+qIexHc&!aip9<jUL<=q{Shn|<s>#1L;>Sm|#nC0vMHwhBJmK!h&;lHk
q8YZ;^D*4~w_#}#P7xR2qV-2O<~>zBaaq0(I^d}1iJ;*)F-V_sTFo|OJEadA;o=cpXF5M3qOSG%#J78nm!*rihw%Acn$$-9*@5m3
lFZY6Csy}FkT+;ljl2X38ztYbm7I4Q9mvJ?sxh*6cVTs?rn1Lov%K+})G06%N>GOsS5BHT;#<tkm%YVCOx;fve6*^uMIG^d=}l5=
6d*IOwi5WLSr?#9Ti;>({IkF?j*ZM&wS7hR&rxS_A3!8CVdn_DtXVizFBn+WW&<@bHc;p)fF&NW;E~$e?~K_4UW2&@zcpA%m@XXS
nU3q(;ZfXg9wk>vL|W)Gn`Ual;H45>BKWL*L@m%NWD-782f=JjI{dE+4AjQ$PVFA=L{n*?iF-U-cXA+URPAJ)hiQkxhosg^74i6?
7_VMsry?gva!jR3uzKMP9Felbi$0QV(rtpl7J|fr9RWIr80+&Bw~amOaFbDUB?iVwz2!k!>n_!*qpxMuxd98x-L>K-AP`K5Hwr?(
442<^_$^(Gr(Vfg*+gNbD+kN0$#ViIj_Q|vV#|`s0t=?Lr_JtG)m2XS&ChN)QGrwkuY9{S4n{0wI75}$uy`ccTkC2o9Trl3^FYQ~
_FDOjOzvK597ZwfqGjYHn-4{+G&6QRuiM1eDRL{WQ_wOVg%Q7KBc&X@wLE<Fb4AR`^*DFl+r$T=95pQjlpw3Noiin9=7M-i^t;8m
pbt!t=%b;KfjtOZ=LA%63{(hOtT@jka+TKZWGZgyD-QOwLDu-9g0_TW%19x})fs{YB3Dt>UFuLurYe4P`6!2(^XpX@B%n9uiJJVK
x6$$I+ZUP-zqN5(p@abh!&GiBM5jF{&-o2}OD%Btpzo&uGzyJtmSLVrJwhe|R%pG)QEFQ(73`>}3L|PzxCyF&d)Q{3hOBl8@;L`?
TZ9v_T*i#X<fEO74`yg=Zwh^hQ^Y%v!gC&8&2u{X;B3jLvofY0w`U_Xq$i(yYAlDbEubuAa{<{xSupR%FoKgtGoc=86Y3%zg2umF
y9YeI4wQup2SiY3!sOY!D;~R5cg1@v-9BNHWbRvHxxwcH(T@=EGf**|EYAlLwQ%o>JQWKL3a>8lH8rwOh3YR=IEA!eR^X*JxK}dJ
(9Pz@TR>M!zkJ+^BP9|B<@_$8>VxM~TFWaax(2bvUqht%Yg35X;^>15hrzNDwlC8txZXVY<Ll?)#zh$JqjPhYRZ?U?3|f3$s?Vib
(@6Qn*Zs@Xy=sHbiGJRxZ!4#r0?B7ZwfWyKM(j-!r)EVWfrV{}rqAgy(_2NyXQg=&3=&nu^1`*7G>H>-T~VLCm!ZAuv|-{;Te3K3
+Or!3LGeVLc-G<hy!o~PT>Gn$MBpt`jzN-#(3<6E=K055p=5kU5Cn&tDLyqBrRv`H=j^NrU>QgTo&vFyrez>CP|!vQ=7j@=&R^=9
{q7Ny7siy6b*mAJo1r_j>IQtPq~TF3ahnR6cPa;*)Q}j#eQ)QTJ2WX<)lI6Z7jiNR>uNz0iKyVo-|S1rkC$Zj{d(z)*fjJwoZPXg
9a@|F<{q`DI<Tq<-wxWsc2c$QLu^|ghvB4t#j}Sk+e;YB-;B1I5T9Qw2ABOnpXj4PZ@p1lboOF{ON^ONS)!Uy;18gFBQ(=9Juz%-
%d2Hj_VRt`&{0wZqXm+sm=wi_F;swz@{>>vqYem?h_Guu8!#VX!2nqe73Yl{D2H$vm<Vm6PLgSRbs(7o1QcZ8!(3-~JIi{k=BD|n
d7t;|AToi}>QImQ<|LL0i!&Ypf+d*HmMn0}L;%L4iRBb&n38y&F+|H^%z+C~Wt2oxAfzzLYCtubHH08!v}Om4JY&+&zmd5;=rM?@
Dl8b#zW34f;J#m&^}D;dz{Z8&FuHWQl*UsanW6Y#UJH){<DS2ZOWu7oQX6q?3GKz9-`P*Qy?wbuqL@?4kKUQ*x%p1McI#Ar%$}V@
wk>MvMjI(a_4Tt}X+OxF3YL%aMZpM~9~{1+l1ouF0;SeF)8g{Ux$!c$nqAOP$w)#?<G_WHAr^y2c~4xpzbU&cO^I8jvJfbfCAgV(
8!dO8`92LdbcRadG9t3`Hbe@FRWzL?BgWzfvO~|0ZAS=G`XPD+1qBaIgbq@4D+o1^34<nx8o@}nWf8M-w}cK78+YBy+FZlya-n4!
r`VRF1RW_PBGKMCUwTG3>rLimps+>;<?NTXn4TEL%{I@Z=QAfd?7m4IpRg)O{~1T^MEvnL*eUoBte<;-V0_xyAC#b|D5NygB1sZ*
5|gIcFz(t`2&OJZB1p7INWebd)6ft<Kqg<P6Yzp;+!G&9XK&vx$LV&IodnAI`G-;PfFal4<uUA<iOeXZkjkcPU(@Ut^bGZhk8MMM
W_~Y<(WmP7iV=Tww!H{^typV)J~VmOZ6(cC8_K~0d?O?<iK@bwjalSQXHwCGDRqu7r=IsBbAnB2N{Fd-aGwY-hZhhIafLGd)E0rb
M-8hpiKTxu{-0Uh`4F7BPDaXXA^I&Z?%@eOmSeZyg<&6DdvTk~zD_4ZCwG5ybq;oB;9ubO>R`&L=f#SM?-7Hphpqa65kO>*%aZ<I
ht@uw-!@;ZzJCCJHC-ug{}13^yB%vP54%70C1H^Y1ct)+a1eKBuJY6^mLSME29L}Tye1bU|CB#tJGH9h7T{alzAK$#bA_|s%t$gn
M&4=%L)X#O&(-cdivH8wX3S<ohd?StJpZTRDJo~5t&x5*C<XZA7X=?*eYZ2Ls<r8EWa+N?_S5P4_17&l&rJB|6g&Ym*TpVC`(Nd!
zFsX)>O=QGd6T&f?F;@?p2Wz{!kqo>Cl1V;Gv0$y3qnRroA2o>_5Htfzqh4~jZoqb6N?q||0ib<MG!B2xXGYf$Ar-o7=l05zY{0F
R^EFEWQ>6y;99AsGGWF0vA;7!bx1KzGfPcT(@wmcKG>y<>X;aSmWd{LQBc?|==*(KIh-_Vz^?im+J>`MpKsj`mh1uH{@wT50Bna-
F~17qLMCPKkR!lv&<`a`huP%|k}PBm=vffrq#IQAU<yK>P{6FfhMo#B0{Ey%NI-V`z6!nOPt*(d|JU@`4|mPyJpT=TkN01<`BtBF
<L&%D*8Yo8e_uLlPK;&vnMmqVQXmjX{N-9e0<aDF<|=}il>zu~xjcl^nR9lEk{MA0o7DHGgzAvPl%yB_BnxXJe5i1t->YPvG;wV#
XDuYquGhM4l_+~ohxec;kM=MsF(v<RM?SWfug`on4gNsz+g^Vj-k{{00IZNG!9hmSRxB!^2_jNXmXY7pfIUz0n%h(cVjy^t=s}cT
ef@1`;j3j^;3_dFf3hchLim@YA=&eu)f2DjZROa}55d=^%VkUVAa^3osLlcwEJ`*wP*h-}DJr!i%#(YD`vn*{ULj#t@52yIy%{k%
2tpYk--h}J`*_z7qN`8S`u~3^JW=eP3#YO*R7&;QtX|D>{YBs1>V7*uzJC8e?tBaRQIV8AUyHj}+a<3qGRPi`Ws@?+5*h7fa?gUi
duW^FdN7eX=I;YPVFR<yn6;RLjri^H4wxfLF&*HqVqb;BK=>$CVneI?ipX+9zb#LrP*9e=0pXQyn8$R<oVYSK2Y4Sw^~-fl@^J9U
Ojs-+S&~yEJrXG`cJE&;e$3-h3L!4EwMQ}erpecIpq&274u^g1u{(5@+gHV)<&f%Y%V9D%YDj#2*_fV3$p~y*FS44R*aMVO=OV>(
xOO+#ea}@m4YNCT0*-s;MoL0%I@u5$)X6oZh5b+oWr6@QoW}SI%EEntiG)5#ZFm$e<%78{e?to(pj*ZZ*)F=3l>(&^PaOE19HDg@
sL9GgQlOCAU+mmVaVmZN-^?ZjY{gc>flFmX4iLhdU5=7a-RXGv)eS?OBspZ}1#5yuk(PNl4tj8Kf)|j+a5PwTFxN)xWf(uxlJ_qU
O3-xDup~XY^Ly-=(uS)<_F_4?sc8){EN^oz@}QcWpG{pg^7LL|=L6&1VHE~Q8msv`p8=+JvP<^pgqTCp)!<2Smnj~=r>V4M(8=iX
Cev@^dqn4{DtUZ$MlCbJq<E0hJ<*A%R_Z%$AgeDsNnHYnkz$a$3(F=vC9Mh#b*LF8{R<e5Zh2Wa+-yTIqXC8DB4!ytG9`&%hbsb9
M@S~5E<oBpEF6(E18vWpf${dbdt`E$t}v?bfvn68H88nI-BIQ|m&6|9^?FSZ*0_zFX|{7l>nxcKC9#zAL`@Q+GUL)3zBMHt7+KKE
J0_O10x(OOPDY`%=tyXhsT(*TXDN7;s3}*G5c-^qw)AT+JJ|5rFyR&x!cVQ)Ai)*}$qHZ;$tKW>$YBp4^NlaIDTo>}cuE4$fk%-r
^Rv@s1&XMSGzDC;#ULu%U@=Te2(C4>e8DZrN;26IkvS7Vk|vo*Mnt=o&558jWD~mu%S2>RTE7D;7`!=3V3!OtDq13=l%lo92abv{
0R%-9M2@V+7;S1XZPSuq(Z+)vte}FLxvfkhL=iz%lLZ`VDVEVxMhJrGl+_wl(p3~lA~6JH@S%&8F0jO6Wvpg2MXh!kxvQf<OJhpZ
b!61vSg^#|u)ORQa!gb<AxOkgK>L#O8fLZ9;k1+kR2Cu;SAskAHE_pf=%(i9Vhz<`fgzSlnC3a87wxl^lp{+5(0bcbjMFY1{Prh8
L?=D=ERd!q#XIYBQnRU4Nkf1`H8)n}1T&f89|-`@lf?{`4ktj^D|u11se6b9wmQlMfMGrQj#0~897NF?QUu5k5<&Go@*~Oqthov}
d|~Wwp<^nWk?m#7#wn*+&c|!r=_K`w7|Mf$1&~OC+8gIp!LdXIYcHF{)Epst%??G4LZSe>*mU7IEtY^s=(woDwV@b#cM`J>3v7Yb
7A$q4tm^BdIBca|L%okU+v(lvBejk7#8Pgv1dk@n;N)r|b*Z?6$-d7C@lkC%jxv{=!J!%%TOtn}p&Z+c5e1&e53?8~R+0v_H|vaX
jdKb42fcUPijBpyw`H7$p?!l8JQ@~Mb}^Vk#+`}2N^bI)4;=A`1h)sROSGVI+8W+w<miBT%e;d#H;moSeOPzfhtORS(ShR)i=2b5
@e5%Vz>elx&l!tVdE+8jO44R>%oq`_^#d{Gw*o5N7cJrOPFCY3Sjc|B%OpXPEFfBBDxQv;ajxAkXglM6F`V-zviZLkyHI9Qsh%F*
vA-iXBc`@(U{~SQz><NNJ>(Ac9Fc^C+#KfMQ`R>hAFSFKpEkzj-Bo#*u9Mx+&{W#uSlG#d2t=F}HkG*DMnI`YEXp&X$|LOer_lyn
@dF1}F}ti1_4Cd_mgI^D-skPz!VFo}fpoA1H0~Yh1Bw8!6p%!OPD&LAFL;viX=lBHq3hpg7pByrA491G%Z3zL08Ek*Xb*QNi9K_L
x?*=F$K0>gYRpWb%jv-8``%O3P;Z&%ReinI3O*-99wFqWWR?WwROOaKV{*hifR~OY6)_6{n?z@}*3(WsK`jrCvO=W*g|3=)4{_<5
kbTKfZF9%)w9k>_?Hgzle|heK$Fe|K-dg?1_eEwq>0Z(Y73Kw*n{fk<=C}4ag2P}@G$K1h!tz6bE#}2{K1h|3fGgl8P?8bt*v|yy
%}uUyyz23>VTgjo7DX`WwhNjkz)X(2G;2km-&yr$k_(lP^@7ktFTZ_z(JQ@6p<$l{zGyBnOt6?sH*n1gvC70`nA%jNHahQ|)vtSv
;A71!ThD6v{t<w(MVZUnZhv|}HR(R#)@|TgpzvR8k7`)KBa|hb7f2a+YLq6DEgieG)}(6SMl*a>EYwumN0B7Nd2vI)YqhpXI=4?K
8$jMQ9o+*YGz9Pt9#&E;_tb{oo*7~Qd3j<QzAx)o*BLS~^JuCHk%`)Xp$?b%T3j<yykBzH%eI%F7hzoIgnN+;NOS@PZD`PhXD*yZ
B~Yr6uHC{gD^HF97fcuu+$iQf&9i6iCtyId#^*zG59QYS&qhXYq0xlh1QTQmnB4t0HqMh^MO+!s)n0DGGMY$eN~h)HxTGOB-KsAg
V%kiwUG{1nP??P#tXNl&zOXwN$_YM1l*dDn%ZgNv8e^?3U1qbHZYK>#dlZM{ZJ%u`Z;XYdPD^a!v}~oVjZSRVAcjCCB{Dgpu5=eg
5ap6Hije}*Q0{I>7v_L-zGpMeTx=X6&h+PwM7ixfIRf)~b5QPsQ%dHY)-5hqRI`3TiiTdPsun_7@yYe_T$5U?wqS%b+ES&o^X*+z
*u%V=|2M*KICSkG-Sujst$j3hIH{A;hNnG?_>=UM7BLV;A_&Oub-9`1SOJbwMTAkMKCclA@#ghiDD)nlsoE9D?Qs?NjIBE4Ba<!s
iFvTG&#Cm*ksrtmc3-G#RhBy0lvGfWv8_G9L$5tBoA6IIash#yNs6kW@r<C*xOkVj0E7wM+BoDPsouaw)k2ys89~GVx~@0oelUf?
`-4<Y4p21!<rHBQWCSEv;|eG=$j!WJL4f7QIRcn53Ghiwj%J{lNEyT`V&Hoi95BvaX84m{xDRp*igTzu4^{OPOmH0Jso=cQ$i_k&
+5pWA{Lcy_%}^?esKzt3TrsxBvH^-_1;&(fj0S(kr1qe0YQ&|X?cPs~BCHfq5pd^ZC%KRoNfd-xB9cW?MFMXFglq(13LDR~F^Xet
q^T@aimik(hR9eoSfHSC+mSHU;bVHJ&14-ASSUmoRfu~gnqxQsZ)(>^%E4Lk!FO1tLdkpg7x8{*5)AQz=)s_<HWTiq;8FRn$obPm
`nC0Kt*s=q&mP&OX{vO_B*~#q6rc|}^(yp!KM3CbV#n8k>>T_$*pV%tb}z;ku+X%ANQs`9^lE){1$g7FUW5-kVy-M1B*p2jwYks?
V9g8?SqMnWR$0_%TIOljd+M1Z;n7@nLkYrJCR1`J=rR`G2i}4166`i(d&Na_-YBa}+|D}W4$&VWo*oeP{}f)=vDzb{hjscByMQuo
L){E90Td8Eg{uOC31Im^qJ=sKq$*Eu2ei!aBJwoTzRPMZZb40)fLrLzK=I!eg3K8Tskw=KEWm>b;0UrG=X$e+^0^yk03c~MM#JkE
x~_N2F!A9>t%a(O2%ri3J^)k-)^#p%9xhpc(-3tq77HX;sE|?$ND4^CDI&=rqQp@o`g*=5Ss$eHGf?u!O-Kj0^?`q*nFzoSIXMP(
>`byE2#XO|MOY}17_;VOa`8W`W5<~A_EkLxq&sKku9==#oSy!ZB$n&Av*goN9U6G!T>&2?>(b3WDGWsoTBeguJsoeXDoMVtdD8XA
t*F890Sd%&2gU*N_D?<B=5Yd6!CdB&G4F`@&Pw_@VwkPGWoA~fNkyHWny@?ep_xp{0@Wg$rZNk4;}2bAl<*$19+cm*a@?W5wB@4F
R#+oi3mKV?L*v&GcXv17*s+(4M{6=Lv5619ffvl_woP!l>sUZh#3stjm888cbu=j{h}KyO<}(8@q7BwY>ohdBGYeHqZKDN4E>goV
Vpb_u%34awloT<;O6Ijhw5o(_919zc13}C(t7{UL#sOaz=I&_uoGkT{4u(Ap=q_=a@W)>FfX^}AB9K|u@}<Kz<fVcvWS}wP;Ran4
Mlr8C3w-e{EmUK@?J(BQtDrYQ1;-`O;NXUQakb<OT$&a?0z}_aDKlypRFr~NzF~tN?Ky_>k2(XhV~L3`<ERBgx0#7B7tkzgLsL9!
w~u2`zDyKtc>ZU4*-;`3qq;z_-%=Jc{}PU^1&5_m91v71Dn-qSgM|eVQBhEAz^cqJ$)mDAVreCT7mUj>hiW@<iE|$6R24=k-yjCE
`buR-S}8^<hUHAEc3~(P0d^W`1Cad5F?A|~oez#u&r9w0H4XZc6MbmYk+xEMc9_h&9q_qoiK3wd%;YyDGbmm?Ly91qd<}p{MhBYO
wLNyXt#XG}U8NP@E=vN-OSA&SpvyxP6+_)J5d19AFE2f_jSX|{8%Fq}1z{4}t|OjXhn)#DEsK}eClckZru^K>$*7wGS_O}qgOr%j
$r29ObUsEF*L^^E`PzmJy-50sZrsFa$=gbhNi3uitio!ku~=*eePD(MoJrlZch#?Ppd4{Yp2!>t0Qw-Mpb`*gJ@>v1HekT@pem6m
ObZbpbUzgz8W1$gdU`5LvLsz^j#}`}N;x1&rivr7lv$%!GZX6N1V=~>)ww1Rh;~Nqoo@oLgLzp9&YEV8Z)Oh|XdKk^UC_|WsBa-D
5gW>`T2b)gkcE5RQ*YkNW7E^InCoF!oFOC{O42g4-8jTyji#nua{&Ae3xOtC4BP;$&v92sD&>KIfF?n>=*zgGV^|A}vtWUdWCW6I
fekVdWC-E1iYQvN6j~)^fHw|d$q?OG40CkFw>FyODbQl85mAbW=1?CC$k1rD$$>DIO@&0ng=R8ijbw`;!G%dOl#(<-pp7V7NHIJm
W-|)ZbVel#q9!h?Qp;q~NU#<Nu~?i>NwxaR!yIOeR-gu8T$Sva1ev&iSn-gV7z(jSKv)9wlC{RUG!Q9kdTyq&`^y+FhEz!yn_;(F
Qbdtg0f@US=2DtQ5lBS?l1i6JS$==Pxi+0KT`I|TPFcEGZiY}4m|zK6-e!dpcn%cE&m@Jw&>N*l3hKbbm<(A&2Gbb~h$tc$#bDhh
Yzagx3l)ISjmRbdvp||9FSAk$ELpNFQDioYS!C~Fa%R?3$lz#<K7r@<e3CDHoAdhC^hLu)+QiN#`Iv3WX{5&3^`1w)<s**1c0mky
@tU(m7@X^oyS-c=dFA)dSQ`R0Dkr5J{3_7*pB^)lMBK1w`}>2Bo8)aIVh`qnUh`yZiAIUnnun>jmULHzkS|RK;_6S8y1M(zo_<3B
1Jlnbu)b52mv!>fQ|Y$XZjubgjDbDf&m%XZ9PDISPo@$CgGwI~cSK|bk|_a5h>9dwNNun!cR)SR6KIK+h-)WWK1kGVAC!Xf-n{SS
pIu?PL%sx24!upo)d&lK%zF|YSV_QKhUhF(EFhp?aP=q&7JL$v3m(r4CPTT1c6b%F(4n|q2a)AOl_<nOQ-)SSNMj*bh@hg$>tvOY
Ml4wn7BT1B%Y)8!;5hac2afWO2ul#1W>jqvaheC$03{;nJ0Z5<Wen!&yao_F%X(unTe=-@RwDROrMGMe%#aVLZBsJdNQj?hYH)nM
)RAVue7MJn155{UaZF5stv(sFU4)C{uTD1iRGmvSB|4|Sys{qVz%^h{4-<xZ5UlP%`Z)$DhY^8Gd(93S06za-qLljj*u57UYHB7Q
1*XXDN<-2^sp@BNh1mB73FL<EES+KF5{QNm&B@0crAR6iPJ44ub=)44gKc&c*2FiRL5$_AQP`>$F|eUUNXoG+Vgamg5jdJU-7vbj
!Qw1fgtlt6VSU%bKr<LMiK{vp!oO;0wl{JOieJYe5H2SXG}NFDAZsE>9__(ppJ}p0n*cpf@QCV}*OzyF(6buA(xstNO*wv8;367f
zabB%dp@d`sv<56Z2}?Jp-~djDF9%d7I{#yV;Csz(MBMM!39SWtVUy11XN<FMzSerR8tayED;1mOr-&gQBoo@A&t1MWJW}4P%W!l
1Vzh1V8LPvsxo)5)=DahsTiQD!UC2;si1+hiFQga6XtwAS{T4*L9~JJCE=`^3Gyy#J&bA^prO-xDbrdr%dlRI;ghRk67fz}%5o<l
IM)QR4aw$mBtb?P(NSoc!r;M5jxw&TP&KQbCkwW1o(Q4zqtQOKR7cy{WYN%ihkFR3D<iI^N2@yb<<>AI?N|+jo#*8oaJ`j<Dh%?V
^q1;diMtbiETMtvmPa!fDl04qc#tAQfH5;PB|C@F$;r9l9e1a}(uF5t6+;%J*yNn5B_uHGfYqmd{JZLn7}nR&(<~?pZHNYPdPYrL
6J36aydYu-sE`tA1hdeXfmtGq!bbOo2r@wjK2x*;MUZ4&AXH7>!P}@M(K)99;i9(y=ny)a1z--mdjh5m2x$f8PQ6YLc>t*))hQ&3
w2IOz1q8r57L8&yQqrXWEk-^P6P#Hg;L8c=WZn?j+{=a~1Z)>6a=|K@-$<rGkQq_?6)?L|o?cAUaR;yNBMUWtrB>*Qh8h&V(w`C#
E-n7ez}8T%czV{O0Lxp1O#%`n2QBvBd?e6L5Wsd>QXiUB3{)3lGF%w7>@pI1&1QlxNRtq(CdsjfYIHU4KS!o?xXbH~{6^U1cFyWf
Tag2lSmJwGNY)~zvw2d*BpiYWAnaz^a3X1>)N7|xk|=!h<^bka{3@6M$TOD{D>FnyGnNCV5a4t`#6_<-?)-Vc`nsY+jPvJ}22Rly
`s%Tb=QyRI>2A}^)GXtj7WG(DM0}&=B8IuHjRTfxWyFkg#nukOK~ho@@vTG!7hr-55EDRz?D6uNnE6ogfJd)vOJ=6s#0(UUi$o+3
p!mv_Axt=X3zkm;0o=c3kemp~S%cdHqrA7a`BxJnDx*Xtm9)BuWEkp$KN!cf>)fWY7K>Q(;%j=vl@p0hN#L%`UGq$(lgL1V3%SW3
4{GCqc)1SY2Zun>K;&m{Jw1EpVPPM6=UQfwcSGB26;T1f>%iC#!Av6SYH-2`%P{<PFS9YjS1w}pjTEhhX=<4C!y2Q5E%WcQGft(G
h09_TXO}5+(k~$BLwgS`lgzSn)jgyZpbTIr5mA!Y5DzPI_PAaw0q+hC#L8c?Q#nu^A!AV~pyld?UIzH$`Sne6q^g1BVNlnbbgkx?
ePf!E7C=6GUW^7)a+D_rcwMyk=A#t)Brvn8FiZkwW(BxvF@nOG3gIAJmQfW#tkT>)3mkj%TWVv7wq=D(v7@m~gv5A?Ny(FgsmgAF
yA2pqpxiRlF-a28UQLFyD?C>U1h}}Aws5RAnbD&p?C2qP%iU%JiH?xv?cb#G@N+0mK?PQJ2iBYUKrW@$Q%uZYC-=Bpp>Qw_1%F7P
ZzZz@>NoV(x6U}1cf@8c(BP;9&lAmAaFmoIS(e3vwo6e;g6fxG)V%Q-$h$e#TS6Ad;XO|cZt_|v#L+HrM%JK2lLg4t<ZRC9aSDaS
0+Yy2OeHAMy1BeLafiuy9E#@?QELz-<=~A%MTCoYYlOrQq30^OI1Pe$xOyO#$W07jO(&F4GWl7>mm0e#niTKH5n^JC+yNPV>T3jG
iZ+J=R`eX?W?904N#Z_|x>IL@#`wI^wvfTyFnw|`!9+3*!3k*?3dyi(<2imP3puOjF^Gy5YD8QIiI{>|I#8g}irEW9MI{(QL&Kpm
m?9vHq~knXjAm8m16DKzt{4oE%!8+pH<%q0jAGddM{1;?4(d}pipnP8nxKh0#u;d1T_mC|m<=dUTSP%%&SyvpA*5!|)Lq2DMTl*p
f>PDjHHHYcYB5mRr8AAlUuO`=)u-YicoZ1J)g4MDl@>rw@O5u)oOj0hLw{#OiNO#>5jC|=AZ~52i1w;X8&4)!kCiJ0QDCp1ViR!U
t@I6ME&l)^d}6au14t;gS3D*N+=NG@Zk}AQT}Ih}6@ZFnoj6m1AYq|W3Jg4!5|}|}YGy&_z2&12)StndhxdIn1C}mgCZOUYOl6Wt
m7H9$Hd(DaMPs^xaS95lbf{LAHJy|%EoC)E*kVcZ$qdoCOSYkgN~@MA$W})5^}|znIcDrP6_HZ9v4Yi)R6;0>1px9BzfEiuYF8)+
=P)G8Ck`k-EZ>M~k0sB)nOU(TBns_TQD^66(S?tK5X}(16bqk6jj1n2H3Uc^9nHHmL^*=sxk)1oE@^sF@Ji&1x<Td8qR}&nn#?%3
GMCZgLl~<KXD5vqqpl0FX@inmCdH{aMNMI|e@|ivNhG1@3FNpu4JkUL=@9*esl<_j$iB~u^HHnX8hz&+Ycm>Zy_ll~Dw%Tv$o-Jt
f<{I_)yzQ24oI8Gd8WrWNDzQel%j}U%IF@Go)0IRbg`JHd&CXFq-07>qqIy$GU`P7i%_<l({Fiib}ZP8X->WeLW06usS7|f>1G++
A1wfHpqgXE?cJ-&sUbAItbFJ*y~t?5li50X!2Cjch#hWniYof5%WYw3Rw>fQvR#bzaC}c`0*Epre6x_vmo4>0H74H4w%LP)d8e4e
!?>qy{vZ|tAf!3%W(_DkxzOKgXBOlV8xv?viW~x<TbIR6;RBkg*|Ai8?OplZ_c_C87EdJW<(Peu+8NWU%rxD``*yi~O&H0lN=js~
xxQ>n9VJM4o}o*eKsO`Iy#j12m0J{{>jg7|lJ0}Db&DXBkW?xb-);PAj{D6etoUeJ>}~L2jH|~<%NCo_86ISrtGkVG<2}j;3s3p+
0mYEDq!u9-L?@C<nvawXic<7gZKu<yR^Ft9BZm4c(J>Q5>oATCOV<NKv!nq`-!|5sdI5!TH?b*tu)Iji=+(n=5QN~%omAFNr1wp;
M3%`G2TYCGo7@~=L$p_(1rtE`<wPcigTL2f!SX}pAcl|1ul2BG|A`uB>9Q8GL?pfpUmEhq@gEdcW)}nDw*KZ3CaIgq3<MZxPxDQU
Sb`Sy5mS8S@P8OU{xCw35C0c(ML1B9_OU7L
"""


@dataclass(frozen=True)
class ProbePayload:
    stage: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    system_chars: int = 0
    tool_schema_bytes: int = 0


@dataclass
class ProbeResult:
    stage: str
    model: str
    ok: bool
    started_at: str
    ended_at: str
    elapsed_seconds: float
    client_request_id: str
    atlas_request_id: str = ""
    cf_ray: str = ""
    trace_id: str = ""
    http_status: int = 0
    headers_received_seconds: float | None = None
    first_event_seconds: float | None = None
    event_id: str = ""
    finish_reason: str = ""
    content: str = ""
    error: str = ""
    system_chars: int = 0
    tool_schema_bytes: int = 0
    request_bytes: int = 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load_embedded_profile() -> dict[str, Any]:
    compressed = base64.b85decode("".join(PROFILE_B85.split()).encode("ascii"))
    raw = bz2.decompress(compressed)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != PROFILE_SHA256:
        raise RuntimeError(
            f"embedded profile checksum mismatch: {actual} != {PROFILE_SHA256}"
        )
    profile = json.loads(raw)
    if not isinstance(profile.get("system_prompt"), str):
        raise RuntimeError("embedded system prompt is invalid")
    tools = profile.get("tools")
    if not isinstance(tools, list) or len(tools) != len(TOOL_NAMES):
        raise RuntimeError("embedded tool profile is invalid")
    return profile


def build_minimal_tools(tool_names: Iterable[str]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "Compatibility probe tool. Do not call it.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
        for name in tool_names
    ]


def build_payloads(stages: Iterable[str]) -> list[ProbePayload]:
    payloads: list[ProbePayload] = []
    for stage in stages:
        user = {
            "role": "user",
            "content": f"Reply exactly: {EXPECTED_CONTENT}. Do not call tools.",
        }
        if stage == "direct_stream":
            payloads.append(
                ProbePayload(
                    stage=stage,
                    messages=[
                        {"role": "user", "content": f"Reply exactly: {EXPECTED_CONTENT}"}
                    ],
                    tools=None,
                )
            )
        elif stage == "minimal_tools":
            tools = build_minimal_tools(TOOL_NAMES)
            payloads.append(
                ProbePayload(
                    stage=stage,
                    messages=[user],
                    tools=tools,
                    tool_schema_bytes=json_size(tools),
                )
            )
        elif stage == FULL_STAGE:
            profile = load_embedded_profile()
            system_prompt = profile["system_prompt"]
            tools = profile["tools"]
            payloads.append(
                ProbePayload(
                    stage=stage,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        user,
                    ],
                    tools=tools,
                    system_chars=len(system_prompt),
                    tool_schema_bytes=json_size(tools),
                )
            )
        else:
            raise ValueError(f"unknown stage: {stage}")
    return payloads


def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def json_size(value: Any) -> int:
    return len(json_bytes(value))


def parse_sse_lines(
    lines: Iterable[bytes | str],
    *,
    started_at: float,
) -> tuple[float | None, str, str, str]:
    first_event_at: float | None = None
    event_id = ""
    content: list[str] = []
    finish_reason = ""
    for raw_line in lines:
        line = (
            raw_line.decode("utf-8", "replace")
            if isinstance(raw_line, bytes)
            else raw_line
        )
        if not line.startswith("data:"):
            continue
        if first_event_at is None:
            first_event_at = time.monotonic() - started_at
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            body = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        if not event_id:
            event_id = str(body.get("id") or body.get("request_id") or "")
        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        delta = choice.get("delta") if isinstance(choice, dict) else {}
        if isinstance(delta, dict) and delta.get("content"):
            content.append(str(delta["content"]))
        if isinstance(choice, dict) and choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
    return first_event_at, event_id, "".join(content), finish_reason


def extract_response_ids(headers: Any) -> tuple[str, str, str]:
    atlas_request_id = (
        headers.get("x-request-id")
        or headers.get("request-id")
        or headers.get("x-correlation-id")
        or ""
    )
    cf_ray = headers.get("cf-ray") or ""
    trace_id = headers.get("traceparent") or headers.get("x-trace-id") or ""
    return atlas_request_id, cf_ray, trace_id


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        values[key] = value
    return values


def load_api_key(env_file: Path | None) -> str:
    api_key = str(os.environ.get("ATLAS_API_KEY") or "").strip()
    if not api_key and env_file is not None:
        api_key = parse_env_file(env_file).get("ATLAS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ATLAS_API_KEY is not set"
            + (f" and was not found in {env_file}" if env_file else "")
        )
    return api_key


def make_result(
    payload: ProbePayload,
    model: str,
    started_at: str,
    started_monotonic: float,
    client_request_id: str,
    request_bytes: int,
    **fields: Any,
) -> ProbeResult:
    return ProbeResult(
        stage=payload.stage,
        model=model,
        ok=bool(fields.pop("ok", False)),
        started_at=started_at,
        ended_at=utc_now(),
        elapsed_seconds=round(time.monotonic() - started_monotonic, 3),
        client_request_id=client_request_id,
        system_chars=payload.system_chars,
        tool_schema_bytes=payload.tool_schema_bytes,
        request_bytes=request_bytes,
        **fields,
    )


def probe(
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
    payload: ProbePayload,
) -> ProbeResult:
    started_at = utc_now()
    started_monotonic = time.monotonic()
    client_request_id = "opus-probe-" + uuid.uuid4().hex
    request_body: dict[str, Any] = {
        "model": model,
        "messages": payload.messages,
        "max_tokens": 32,
        "stream": True,
    }
    if payload.tools:
        request_body["tools"] = payload.tools
    body_bytes = json_bytes(request_body)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body_bytes,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Connection": "close",
            "User-Agent": "atlas-opus-compat-probe/3",
            "X-Client-Request-Id": client_request_id,
            "X-Request-Id": client_request_id,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            headers_received = round(time.monotonic() - started_monotonic, 3)
            atlas_request_id, cf_ray, trace_id = extract_response_ids(
                response.headers
            )
            status = int(getattr(response, "status", response.getcode()))
            common = {
                "http_status": status,
                "headers_received_seconds": headers_received,
                "atlas_request_id": atlas_request_id,
                "cf_ray": cf_ray,
                "trace_id": trace_id,
            }
            if status >= 300:
                error = response.read(1000).decode("utf-8", "replace")
                return make_result(
                    payload,
                    model,
                    started_at,
                    started_monotonic,
                    client_request_id,
                    len(body_bytes),
                    error=error,
                    **common,
                )
            first_event, event_id, content, finish_reason = parse_sse_lines(
                response,
                started_at=started_monotonic,
            )
            error = ""
            if content.strip() != EXPECTED_CONTENT:
                error = (
                    "unexpected content"
                    if content
                    else "stream completed without assistant content"
                )
            return make_result(
                payload,
                model,
                started_at,
                started_monotonic,
                client_request_id,
                len(body_bytes),
                ok=not error,
                first_event_seconds=(
                    round(first_event, 3) if first_event is not None else None
                ),
                event_id=event_id,
                finish_reason=finish_reason,
                content=content[:500],
                error=error,
                **common,
            )
    except urllib.error.HTTPError as exc:
        atlas_request_id, cf_ray, trace_id = extract_response_ids(exc.headers)
        error = exc.read(1000).decode("utf-8", "replace")
        return make_result(
            payload,
            model,
            started_at,
            started_monotonic,
            client_request_id,
            len(body_bytes),
            http_status=exc.code,
            atlas_request_id=atlas_request_id,
            cf_ray=cf_ray,
            trace_id=trace_id,
            error=error or f"HTTPError: {exc}",
        )
    except Exception as exc:
        return make_result(
            payload,
            model,
            started_at,
            started_monotonic,
            client_request_id,
            len(body_bytes),
            error=f"{type(exc).__name__}: {str(exc)[:1000]}",
        )


def parse_csv(raw: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("at least one value is required")
    return values


def resolve_stages(raw: tuple[str, ...] | None, full: bool) -> tuple[str, ...]:
    stages = raw or DEFAULT_STAGES
    if full and FULL_STAGE not in stages:
        stages = (*stages, FULL_STAGE)
    unknown = sorted(set(stages) - set(ALL_STAGES))
    if unknown:
        raise argparse.ArgumentTypeError("unknown stages: " + ", ".join(unknown))
    return tuple(dict.fromkeys(stages))


def write_results(path: Path, results: list[ProbeResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [asdict(result) for result in results],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def default_env_file() -> Path:
    hermes_home = os.environ.get("HERMES_HOME")
    return (
        Path(hermes_home).expanduser()
        if hermes_home
        else Path.home() / ".hermes"
    ) / ".env"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="required safety gate for paid live requests",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="add embedded real Agent schema/context stage",
    )
    parser.add_argument("--stages", type=parse_csv)
    parser.add_argument(
        "--models",
        type=parse_csv,
        default=DEFAULT_MODELS,
        help="comma-separated model IDs",
    )
    parser.add_argument(
        "--atlas-url",
        default="https://api.atlascloud.ai/v1",
    )
    parser.add_argument("--timeout", type=float, default=135.0)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=default_env_file(),
        help="optional dotenv-style file containing ATLAS_API_KEY",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON path updated after every completed request",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirm_live:
        print("Refusing paid live requests without --confirm-live", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be positive", file=sys.stderr)
        return 2
    try:
        stages = resolve_stages(args.stages, args.full)
        payloads = build_payloads(stages)
        api_key = load_api_key(args.env_file)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Probe setup failed: {exc}", file=sys.stderr)
        return 2

    results: list[ProbeResult] = []
    try:
        for model in args.models:
            for payload in payloads:
                print(
                    json.dumps(
                        {
                            "type": "probe_started",
                            "stage": payload.stage,
                            "model": model,
                            "system_chars": payload.system_chars,
                            "tool_schema_bytes": payload.tool_schema_bytes,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                result = probe(
                    api_key=api_key,
                    base_url=args.atlas_url,
                    model=model,
                    timeout_seconds=args.timeout,
                    payload=payload,
                )
                results.append(result)
                print(json.dumps(asdict(result), ensure_ascii=False), flush=True)
                if args.output:
                    write_results(args.output, results)
    except KeyboardInterrupt:
        if args.output:
            write_results(args.output, results)
        print("Probe interrupted; no background run was created.", file=sys.stderr)
        return 130

    return 0 if results and all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
