def parse_pair(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError("pair must contain =")
    key, raw = value.split("=", 1)
    if not key:
        raise ValueError("key is required")
    if not raw:
        raise ValueError("value is required")
    if key.startswith("_"):
        return key.strip(), raw.strip()
    return key.strip(), raw.strip()
