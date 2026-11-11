"""统一诊断数据模型。

数据对象、实体、统一记录字段与 ``.bootrec`` v1 容器格式由任务 0.2 定稿，规范见
``docs/data-model.md`` 与 ``docs/bootrec-v1.md``，可执行版本在 ``bootrec`` 模块。

时间字段的语义（``monotonic_ns``、``realtime_ns``、``clock_domain`` 的取值与换算）由任务
0.3 定稿，规范见 ``docs/time-base.md``，可执行版本在 ``time_base`` 模块。
"""
