# -*- coding: utf-8 -*-
"""幕一重建节点的自足任务书（无外部引用，逐字契约）。"""
from pathlib import Path

PKG = Path(__file__).resolve().parent
ABS_FIX = PKG / "fixtures"
ABS_REF = PKG / "checks" / "data_ref.txt"

N01B = f"""任务：在交付目录创建 data/orders.csv 与 data/refunds.csv。
第1步：读文件 {ABS_REF}（内容一行版本号，当前 v1）。
第2步：把 {ABS_FIX}/<版本号>/orders.csv 与 {ABS_FIX}/<版本号>/refunds.csv
原样复制为交付目录 data/orders.csv 与 data/refunds.csv（data 子目录自建；
逐字节一致，验收校验 sha256，禁止手写或改写）。"""

N02B = """任务：交付 loader.py（函数名逐字一致，缺一即验收失败）。文件必须包含：
class InputError(ValueError):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)

def load_orders(path):
    ...  # 读 CSV（utf-8-sig 容 BOM；csv.DictReader）→ list[dict]，键=列名

def load_refunds(path):
    ...  # 同上

规则：容忍合法引号字段（含逗号）与额外命名列；表头重复或缺必需列
（orders: order_id,region,currency,amount_cents,status；
refunds: refund_id,order_id,currency,amount_cents）→
raise InputError("columns_missing")。只用标准库。
交付前自测（必须执行）：
python -c "import loader; loader.load_orders; loader.load_refunds"
只交付 loader.py 一个文件。"""

N03B = """任务：交付 validation.py。内容：
from loader import InputError

def validate(orders, refunds):
    # 返回 (paid订单列表, 合法退款列表, cancelled订单数)
    ...

判定规则（13 错误码，raise InputError("<码>")）：
order_id_duplicate / refund_id_duplicate（ID 各自非空且唯一，空→
order_id_empty/refund_id_empty）；unknown_order（退款引用不存在的订单）；
refund_on_cancelled（引用 cancelled 订单）；currency 仅 CNY/USD
（错→currency_invalid）；status 仅 paid/cancelled（错→status_invalid）；
amount_cents 须 ASCII 非负十进制整数允许前导零（错→amount_invalid），清洗
转 int（支持超 int64）；region 非空（空→region_empty）；币种不一致→
currency_mismatch；同订单累计退款超金额→refund_exceeds_order。
cancelled 不进 paid 列表但计数。只用标准库。
loader.py 在 inputs/ 来源目录（sys.path 已含，直接 import）。
至少自测三类：order_id_duplicate、refund_on_cancelled、amount_invalid。
只交付 validation.py。"""
