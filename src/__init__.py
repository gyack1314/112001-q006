"""网络资源目录数据接入与规范建模包。

模块:
- schemas:报文 Schema 注册表(显式版本)
- mappings:映射注册表(显式映射版本,枚举/单位/口径规则)
- ingest:原始报文接入(原样保留)
- normalize:规范化引擎(兼容逻辑 + 隔离诊断)
- aggregate:容量聚合(共享资源按归属口径计一次)
- snapshot:期间快照(钉住映射版本与事实版本)
- compare:快照/映射版本对比
- queries:查询与溯源接口
- store:SQLite 存储层
"""
