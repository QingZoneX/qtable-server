import json
from datetime import datetime, timezone
from openai import AsyncOpenAI
from app.core.config import settings
from app.services.encryption import decrypt_api_key


SYSTEM_PROMPT = """You are a professional data analysis assistant, specialized in helping users analyze tabular data and create new records.

Rules:
1. Answer in Chinese (unless the user uses another language)
2. Answers should be concise, professional, and insightful
3. If the data is insufficient to answer, explain what additional data is needed
4. Supported analysis types: data summary, trend analysis, anomaly detection, correlation analysis
5. You can suggest visualization options (e.g., chart types)
6. When users want to create new records, extract the field values from their request and return them in JSON format
7. For record creation, ensure all required fields have appropriate values based on the field type
"""

CREATE_RECORD_PROMPT = """You are a data record creation assistant. Your task is to extract information from user requests and map them to table fields.

Given:
- Table schema (fields with their types and options)
- User's natural language request

Your job:
1. Analyze the user's request to understand what record they want to create
2. Map the extracted information to the appropriate table fields
3. Return a JSON object with field_id as keys and the corresponding values
4. For fields not mentioned by the user, use null or appropriate default values
5. Ensure values match the field type:
   - text: string
   - number: number
   - select: option ID (from available options)
   - multiSelect: array of option IDs
   - date: timestamp in milliseconds
   - member: user ID or name
   - checkbox: boolean
   - progress: number (0-100)
   - attachment: array of attachment objects

Response format:
Return ONLY a valid JSON object like this:
{
  "field_id_1": value1,
  "field_id_2": value2,
  ...
}

Do NOT include any explanation or markdown formatting. Just return the pure JSON.

Important:
- If the user's request is unclear or missing critical information, still try to extract what you can
- For date fields, convert natural language dates to timestamps (e.g., "tomorrow", "next week")
- For select fields, match the user's description to the closest option ID
- Be intelligent about inferring values from context
"""


def table_to_markdown(records: list, fields: list, max_rows: int = 100) -> str:
    if not fields:
        return "(No fields defined)"
    header = "| " + " | ".join(f["name"] for f in fields) + " |"
    separator = "| " + " | ".join("---" for _ in fields) + " |"
    rows = []
    for record in records[:max_rows]:
        cells = []
        for f in fields:
            val = normalize_field_value(record, f)
            cells.append(str(val) if val is not None else "")
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, separator] + rows)


def _format_timestamp(value):
    if not isinstance(value, (int, float)):
        return None
    timestamp = value / 1000 if value > 10_000_000_000 else value
    try:
        return (
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
            .date()
            .isoformat()
        )
    except (OverflowError, OSError, ValueError):
        return None


def normalize_field_value(record: dict, field: dict):
    field_id = field.get("id")
    raw_value = record.get(field_id, "")
    if raw_value is None or raw_value == "":
        return ""

    options = {
        option.get("id"): option.get("label")
        for option in (field.get("options") or [])
        if isinstance(option, dict) and option.get("id")
    }
    field_type = field.get("type")

    if field_type in {"select", "member"}:
        if isinstance(raw_value, list):
            return ", ".join(str(options.get(value, value)) for value in raw_value)
        return options.get(raw_value, raw_value)

    if field_type == "multiSelect":
        if isinstance(raw_value, list):
            return ", ".join(str(options.get(value, value)) for value in raw_value)
        return str(raw_value)

    if field_type == "date":
        formatted = _format_timestamp(raw_value)
        return formatted or raw_value

    if isinstance(raw_value, list):
        return ", ".join(str(item) for item in raw_value)
    if isinstance(raw_value, dict):
        return json.dumps(raw_value, ensure_ascii=False)
    return raw_value


def build_table_summary(records: list, fields: list, max_rows: int = 100) -> str:
    visible_records = records[:max_rows]
    field_summaries = []
    for field in fields:
        field_summaries.append(
            f'- {field.get("name", field.get("id", "unknown"))} '
            f'(id={field.get("id", "")}, type={field.get("type", "unknown")})'
        )

    return (
        f"Total fields: {len(fields)}\n"
        f"Total records available: {len(records)}\n"
        f"Records included in this prompt: {len(visible_records)}\n"
        "Fields:\n"
        + ("\n".join(field_summaries) if field_summaries else "- None")
    )


async def get_ai_client(api_key_encrypted: str, model: str):
    """获取 AI 客户端"""
    from app.services.deepseek_helper import DEEPSEEK_BASE_URL as DS_V1_URL
    api_key = decrypt_api_key(api_key_encrypted)
    base_url = (settings.DEEPSEEK_BASE_URL or DS_V1_URL).rstrip("/")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
    )
    return client, (model or settings.DEEPSEEK_MODEL or "deepseek-chat")


async def analyze_table_data(
    api_key_encrypted: str,
    model: str,
    table_records: list,
    table_fields: list,
    question: str,
    conversation_history: list | None = None,
):
    """分析表格数据（异步生成器）"""
    client, model = await get_ai_client(api_key_encrypted, model)
    table_md = table_to_markdown(table_records, table_fields)
    table_summary = build_table_summary(table_records, table_fields)
    user_prompt = (
        "Please analyze the following table data.\n\n"
        f"{table_summary}\n\n"
        "Table sample in markdown format:\n"
        f"{table_md}\n\n"
        f"User question: {question}"
    )
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_prompt})
    
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
    )
    
    async for chunk in stream:
        if not chunk.choices:
            continue
        content = chunk.choices[0].delta.content
        if content:
            yield json.dumps({"content": content}) + "\n\n"
    
    yield json.dumps({"done": True}) + "\n\n"


async def create_record_from_request(
    api_key_encrypted: str,
    model: str,
    table_fields: list,
    user_request: str,
) -> dict | None:
    """从用户请求中创建记录数据"""
    client, model = await get_ai_client(api_key_encrypted, model)
    
    # 构建字段信息
    fields_info = []
    for field in table_fields:
        field_info = {
            "id": field.get("id"),
            "name": field.get("name"),
            "type": field.get("type"),
        }
        if field.get("options"):
            field_info["options"] = field["options"]
        fields_info.append(field_info)
    
    user_prompt = (
        f"Create a new record based on this request: {user_request}\n\n"
        f"Available fields:\n{json.dumps(fields_info, ensure_ascii=False, indent=2)}\n\n"
        "Return only the JSON object with field values."
    )
    
    messages = [
        {"role": "system", "content": CREATE_RECORD_PROMPT},
        {"role": "user", "content": user_prompt}
    ]
    
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=False,
        )
        
        content = response.choices[0].message.content
        if content:
            # 尝试解析JSON
            try:
                # 清理可能的markdown代码块标记
                cleaned = content.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.split("\n", 1)[1]
                if cleaned.endswith("```"):
                    cleaned = cleaned.rsplit("\n", 1)[0]
                cleaned = cleaned.strip()
                
                record_data = json.loads(cleaned)
                
                # 验证和规范化数据
                validated_data = validate_and_normalize_record(record_data, table_fields)
                return validated_data
            except json.JSONDecodeError:
                return None
    except Exception:
        return None
    
    return None


def validate_and_normalize_record(record_data: dict, table_fields: list) -> dict:
    """验证和规范化记录数据"""
    validated = {}
    
    # 创建字段映射
    field_map = {field["id"]: field for field in table_fields}
    
    for field_id, value in record_data.items():
        if field_id not in field_map:
            continue  # 跳过未知字段
            
        field = field_map[field_id]
        field_type = field.get("type", "text")
        
        # 根据字段类型验证和转换值
        normalized_value = normalize_value_by_type(value, field_type, field)
        validated[field_id] = normalized_value
    
    return validated


def normalize_value_by_type(value, field_type: str, field: dict):
    """根据字段类型规范化值"""
    if value is None or value == "":
        return None
    
    if field_type == "text":
        return str(value) if value is not None else None
    
    elif field_type == "number":
        try:
            return float(value) if isinstance(value, (int, float, str)) else None
        except (ValueError, TypeError):
            return None
    
    elif field_type == "select":
        options = field.get("options", [])
        option_ids = [opt["id"] for opt in options if isinstance(opt, dict)]
        if value in option_ids:
            return value
        # 尝试匹配标签
        for opt in options:
            if isinstance(opt, dict) and opt.get("label") == str(value):
                return opt["id"]
        return None  # 无效选项
    
    elif field_type == "multiSelect":
        if isinstance(value, list):
            options = field.get("options", [])
            option_ids = [opt["id"] for opt in options if isinstance(opt, dict)]
            valid_values = []
            for item in value:
                if item in option_ids:
                    valid_values.append(item)
                else:
                    # 尝试匹配标签
                    for opt in options:
                        if isinstance(opt, dict) and opt.get("label") == str(item):
                            valid_values.append(opt["id"])
                            break
            return valid_values if valid_values else []
        return []
    
    elif field_type == "date":
        if isinstance(value, (int, float)):
            return int(value)
        elif isinstance(value, str):
            # 尝试解析日期字符串
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return int(dt.timestamp() * 1000)
            except (ValueError, AttributeError):
                return None
        return None
    
    elif field_type == "checkbox":
        if isinstance(value, bool):
            return value
        elif isinstance(value, str):
            return value.lower() in ['true', 'yes', '1', 'on']
        elif isinstance(value, (int, float)):
            return bool(value)
        return False
    
    elif field_type == "progress":
        try:
            progress = float(value)
            return max(0, min(100, progress))  # 限制在0-100之间
        except (ValueError, TypeError):
            return 0
    
    elif field_type == "email":
        if isinstance(value, str) and '@' in value:
            return value
        return None
    
    elif field_type == "phone":
        if isinstance(value, str):
            # 简单的手机号验证
            import re
            if re.match(r'^\+?[\d\s\-\(\)]+$', value):
                return value
        return None
    
    elif field_type == "url":
        if isinstance(value, str):
            if value.startswith(('http://', 'https://', 'ftp://')):
                return value
            # 尝试添加协议前缀
            if '.' in value and not value.startswith(('www.', 'http')):
                return f'https://{value}'
        return None
    
    elif field_type == "rating":
        try:
            rating = float(value)
            max_rating = field.get("property", {}).get("max", 5)
            return max(0, min(max_rating, rating))
        except (ValueError, TypeError):
            return 0
    
    else:
        # 默认处理
        return value
