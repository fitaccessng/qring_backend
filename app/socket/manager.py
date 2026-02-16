class SocketState:
    def __init__(self):
        self.user_sid: dict[str, str] = {}
        self.sid_user: dict[str, str] = {}

    def bind(self, user_id: str, sid: str):
        self.user_sid[user_id] = sid
        self.sid_user[sid] = user_id

    def unbind_sid(self, sid: str):
        user_id = self.sid_user.pop(sid, None)
        if user_id and self.user_sid.get(user_id) == sid:
            self.user_sid.pop(user_id, None)

    def get_sid(self, user_id: str) -> str | None:
        return self.user_sid.get(user_id)


socket_state = SocketState()
