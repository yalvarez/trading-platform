from pydantic import BaseModel
from typing import Optional, List

class Setting(BaseModel):
    key: str
    value: str

class Account(BaseModel):
    id: Optional[int]
    name: str
    host: str
    port: int
    active: bool
    fixed_lot: Optional[float]
    chat_id: Optional[str]
    trading_mode: Optional[str]
    allowed_channels: Optional[List[int]] = []

class Channel(BaseModel):
    id: Optional[int]
    name: str
    description: Optional[str]

class Provider(BaseModel):
    id: Optional[int]
    name: str
    parser: str

class AccountChannel(BaseModel):
    account_id: int
    channel_id: int

class ChannelProvider(BaseModel):
    channel_id: int
    provider_id: int
