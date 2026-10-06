import time
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 1.5):
        # limit = время в секундах между сообщениями
        self.limit = limit
        self.user_timeouts: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        
        user_id = None
        if isinstance(event, (Message, CallbackQuery)):
            user_id = event.from_user.id

        if user_id:
            current_time = time.time()
            last_time = self.user_timeouts.get(user_id, 0)

            # Если прошло меньше 1.5 секунд с прошлого клика/сообщения
            if current_time - last_time < self.limit:
                if isinstance(event, CallbackQuery):
                    await event.answer("⚠️️ Не спешите! Подождите секунду.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("⚠️ Вы отправляете сообщения слишком часто.")
                return # Не даем коду выполняться дальше

            self.user_timeouts[user_id] = current_time

        return await handler(event, data)
