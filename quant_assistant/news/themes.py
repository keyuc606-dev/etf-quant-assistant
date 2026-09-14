"""账户 ETF 的分析主题映射；与 config.ETF_POOL 完全独立。"""

ETF_THEME_MAP = {
    "561550": {
        "name": "中证500增强ETF华泰柏瑞", "asset_class": "A股权益",
        "market": "中国内地", "theme": "中证500增强",
        "keywords": ["中证500", "中小盘", "量化增强"], "benchmark": "中证500指数",
    },
    "159866": {
        "name": "日经ETF工银", "asset_class": "海外权益",
        "market": "日本", "theme": "日本股票",
        "keywords": ["日经225", "日本股市", "日本央行"], "benchmark": "日经225指数",
    },
    "159851": {
        "name": "金融科技ETF华宝", "asset_class": "A股行业主题",
        "market": "中国内地", "theme": "金融科技",
        "keywords": ["金融科技", "数字金融", "互联网金融", "移动支付"], "benchmark": "中证金融科技主题指数",
    },
    "560860": {
        "name": "工业有色ETF万家", "asset_class": "A股行业主题",
        "market": "中国内地", "theme": "工业有色金属",
        "keywords": ["工业金属", "有色金属", "铜价", "铝价"], "benchmark": "工业有色相关指数",
    },
    "159201": {
        "name": "自由现金流ETF华夏", "asset_class": "A股策略指数",
        "market": "中国内地", "theme": "自由现金流",
        "keywords": ["自由现金流", "现金流指数", "股东回报"], "benchmark": "自由现金流主题指数",
    },
    "159782": {
        "name": "双创50ETF银华", "asset_class": "A股成长权益",
        "market": "中国内地", "theme": "科创创业50",
        "keywords": ["科创创业50", "双创50", "科创板", "创业板"], "benchmark": "中证科创创业50指数",
    },
    "159160": {
        "name": "电池ETF东财", "asset_class": "A股行业主题",
        "market": "中国内地", "theme": "新能源电池",
        "keywords": ["动力电池", "锂电池", "新能源电池", "储能电池"], "benchmark": "电池主题指数",
    },
    "159934": {
        "name": "黄金ETF易方达", "asset_class": "商品",
        "market": "中国内地", "theme": "黄金",
        "keywords": ["黄金价格", "金价", "黄金ETF", "央行购金"], "benchmark": "上海黄金交易所黄金现货",
    },
    "159625": {
        "name": "绿色电力ETF嘉实", "asset_class": "A股行业主题",
        "market": "中国内地", "theme": "绿色电力",
        "keywords": ["绿色电力", "绿电", "风电", "光伏发电"], "benchmark": "绿色电力主题指数",
    },
    "560710": {
        "name": "船舶ETF富国", "asset_class": "A股行业主题",
        "market": "中国内地", "theme": "船舶制造",
        "keywords": ["船舶制造", "造船", "船舶工业", "新船订单"], "benchmark": "船舶产业主题指数",
    },
    "518600": {
        "name": "金ETF广发", "asset_class": "商品",
        "market": "中国内地", "theme": "黄金",
        "keywords": ["黄金价格", "金价", "黄金ETF", "央行购金"], "benchmark": "上海黄金交易所黄金现货",
    },
    "513010": {
        "name": "恒生科技ETF易方达", "asset_class": "海外权益",
        "market": "中国香港", "theme": "恒生科技",
        "keywords": ["恒生科技", "香港科技股", "港股科技", "恒生科技指数"], "benchmark": "恒生科技指数",
    },
    "515880": {
        "name": "通信ETF国泰", "asset_class": "A股行业主题",
        "market": "中国内地", "theme": "通信产业",
        "keywords": ["通信行业", "通信设备", "5G", "光模块", "算力网络"], "benchmark": "通信产业主题指数",
    },
    "159649": {
        "name": "国开债ETF", "asset_class": "债券",
        "market": "中国内地", "theme": "政策性金融债",
        "keywords": ["国开债", "政策性金融债", "国家开发银行债", "债券利率"],
        "benchmark": "中债-1-5年国开行债券指数",
    },
}


STOCK_THEME_MAP = {
    "002698": {"name": "博实股份", "asset_class": "A股股票", "market": "深圳", "theme": "工业自动化", "keywords": ["博实股份", "工业机器人", "智能制造", "自动化装备"], "benchmark": None},
    "002074": {"name": "国轩高科", "asset_class": "A股股票", "market": "深圳", "theme": "动力电池", "keywords": ["国轩高科", "动力电池", "锂电池", "储能"], "benchmark": None},
    "688776": {"name": "国光电气", "asset_class": "A股股票", "market": "上海", "theme": "真空及微波器件", "keywords": ["国光电气", "微波器件", "真空器件", "军工电子"], "benchmark": None},
    "002241": {"name": "歌尔股份", "asset_class": "A股股票", "market": "深圳", "theme": "消费电子", "keywords": ["歌尔股份", "消费电子", "智能声学", "虚拟现实"], "benchmark": None},
    "002130": {"name": "沃尔核材", "asset_class": "A股股票", "market": "深圳", "theme": "新材料与线缆", "keywords": ["沃尔核材", "核辐射改性材料", "高速通信线", "电线电缆"], "benchmark": None},
    "601006": {"name": "大秦铁路", "asset_class": "A股股票", "market": "上海", "theme": "铁路运输", "keywords": ["大秦铁路", "铁路运输", "煤炭运输", "货运量"], "benchmark": None},
    "688290": {"name": "景业智能", "asset_class": "A股股票", "market": "上海", "theme": "核工业智能装备", "keywords": ["景业智能", "核工业", "智能装备", "核技术"], "benchmark": None},
    "600104": {"name": "上汽集团", "asset_class": "A股股票", "market": "上海", "theme": "汽车制造", "keywords": ["上汽集团", "汽车销量", "新能源汽车", "智能汽车"], "benchmark": None},
    "688158": {"name": "优刻得", "asset_class": "A股股票", "market": "上海", "theme": "云计算", "keywords": ["优刻得", "云计算", "云服务", "算力"], "benchmark": None},
    "600843": {"name": "上工申贝", "asset_class": "A股股票", "market": "上海", "theme": "专用设备", "keywords": ["上工申贝", "工业缝制设备", "智能制造", "专用设备"], "benchmark": None},
    "600315": {"name": "上海家化", "asset_class": "A股股票", "market": "上海", "theme": "日用化妆品", "keywords": ["上海家化", "化妆品", "日化", "消费品"], "benchmark": None},
    "002005": {"name": "德豪润达", "asset_class": "A股股票", "market": "深圳", "theme": "家电与照明", "keywords": ["德豪润达", "小家电", "LED", "照明"], "benchmark": None},
}


ACCOUNT_THEME_MAP = {**ETF_THEME_MAP, **STOCK_THEME_MAP}
