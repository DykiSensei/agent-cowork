"""M2 参数实测（文档 §12 M2）。

`policy.py` 的六个值是起步猜测。要把它们变成结论，需要三样东西，
正好是这个包的三个模块：

    tasks.py    固定任务集 —— 没有它，所有参数只能靠感觉
    runner.py   跑批 + 仪表化 —— 每任务至少 5 次，单次运行是噪声（§11.5d）
    analyze.py  从记录推参数 —— 每个参数一句话依据

**这个包不参与生产链路**，只读地包装 Orchestrator。它不能为了好测而改
Runtime 的行为：一旦实测对象被实测工具改变，测出来的值就没有意义。
"""

from .tasks import ALL_TASKS, BENCH_TASKS, PROBE_TASKS, BenchTask, Category

__all__ = ["ALL_TASKS", "BENCH_TASKS", "PROBE_TASKS", "BenchTask", "Category"]
