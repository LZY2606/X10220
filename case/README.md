# NMR 共振经纬离线案例

本包不是通用表格：化合物为乙基 4-异丙基苯甲酸酯（ethyl 4-isopropylbenzoate）。它用于复核化学位移、耦合候选与归属证据室。

* `molecule.json`：原子、核、骨架坐标、预测位移和期望 COSY/HSQC/HMBC 边。
* `proton1d_v1.peaks`：含整体化学位移偏置、芳香对称峰与重叠甲基峰的一维氢谱。
* `proton1d_v2.peaks`：仅修订参考设置；不覆盖 v1。
* `carbon1d.peaks`：一维碳谱。
* `cosvy.peaks`：COSY 峰列表。
* `hsqc.peaks`：HSQC 峰列表。
* `hmbc_v1.peaks`：故意缺少一条可作为证据的 HMBC 边。
* `hmbc_v2.peaks`：局部补回缺失边；只使依赖它的候选过期。
* `malformed.peaks`：导入层必须明确拒绝的格式错误样例。

所有位移原始单元格均保留，例如带空格和单位的 `7.45 ppm`；派生层才转换为数值 ppm。坐标基准来自每个谱文件的 `reference` 头。
