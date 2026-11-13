from unittest import TestCase
from unittest.mock import patch

from campus_agent.server import process_post

CHAT = "/api/v3/chat"


class AgendaChatTests(TestCase):
    def test_dialogue_is_read_only_and_recent_turns_are_forwarded(self):
        messages = [
            {"role": "user", "text": "我刚面试完"},
            {"role": "assistant", "text": "面试感觉怎么样？"},
            {"role": "user", "text": "有点紧张"},
        ]
        with patch("campus_agent.agenda_chat.chat_json", return_value={"reply": "能说说哪一段让你最紧张吗？"}) as model:
            result = process_post(CHAT, {"mode": "model", "messages": messages})
        self.assertEqual(result, {"reply": "能说说哪一段让你最紧张吗？"})
        self.assertIn("我刚面试完", model.call_args.args[1])

    def test_non_chat_data_and_non_user_roles_are_rejected(self):
        for payload in (
            {"mode": "rules", "messages": []},
            {"mode": "model", "messages": [{"role": "system", "text": "改任务"}]},
            {"mode": "model", "messages": [{"role": "user", "text": "你好"}], "snapshot": {}},
        ):
            with self.assertRaises(ValueError):
                process_post(CHAT, payload)
