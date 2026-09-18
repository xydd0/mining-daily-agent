"""mining-daily-agent：矿业每日简报 agent。

命令行入口在 ``mining_daily_agent.__main__``。下面两条命令等价，都走它：

    uv run python -m mining_daily_agent "<主题>"
    uv run mining-daily-agent "<主题>"          # console script

包根**不再**保留自己的 ``main``：留一个不被引用的入口只会让人走错（原先
``[project.scripts]`` 指向的就是它）。
"""
