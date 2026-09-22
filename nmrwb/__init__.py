"""NMR 共振经纬:化学位移、耦合候选与归属证据室。

领域工作台核心包:
- units/parser:导入层,原始表示与诊断一并保留;
- engine:候选生成与证据计算(NumPy);
- service:事件溯源、分支、重放、导出/导入、案例剧本。
"""

__all__ = ["units", "parser", "engine", "service"]
