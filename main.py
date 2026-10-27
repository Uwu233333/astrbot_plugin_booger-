import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from astrbot.api import logger

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.message_components import Image, At, Plain


@register("astrbot_plugin_booger", "小仙", "群友弹鼻屎：游戏菜单、弹射、互擦、排行榜和每日鼻屎王", "1.8.10")
class BoogerPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # AstrBot 会根据 _conf_schema.json 注入插件配置；保留默认值以兼容旧环境。
        self.config = config if config is not None else {}
        # 使用 AstrBot 规范的插件数据目录：data/plugin_data/astrbot_plugin_booger
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_booger")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "booger_state.json"
        # 兼容旧版本：首次升级时读取插件目录下的旧数据，避免游戏记录丢失。
        self.legacy_data_file = Path(__file__).parent / "data" / "booger_state.json"
        self.state = self._load_state()
        self.cooldown = 30
        self._scheduler_task = None
        self._last_daily_date = ""
        self._last_hourly_slot = ""
        self._group_bots = {}
        self._group_umos = {}

    def _load_state(self):
        try:
            source_file = self.data_file
            if not source_file.exists() and self.legacy_data_file.exists():
                source_file = self.legacy_data_file
            if source_file.exists():
                obj = json.loads(source_file.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    obj.setdefault("groups", {})
                    obj.setdefault("cooldowns", {})
                    obj.setdefault("daily", {})
                    # 旧路径的数据成功读取后写入新目录；不删除旧文件，确保迁移可回退。
                    if source_file != self.data_file:
                        self.data_file.write_text(
                            json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                        logger.info("群友弹鼻屎：已将旧版游戏数据迁移到插件数据目录。")
                    return obj
        except Exception:
            pass
        return {"groups": {}, "cooldowns": {}, "daily": {}}

    def _save(self):
        tmp = self.data_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.data_file)

    @staticmethod
    def _gid(event):
        try:
            gid = event.get_group_id()
            return str(gid) if gid else ""
        except Exception:
            return ""

    @staticmethod
    def _uid(event):
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _name(event):
        try:
            return str(event.get_sender_name() or event.get_sender_id() or "神秘群友")
        except Exception:
            return "神秘群友"

    @staticmethod
    def _message_segments(event):
        try:
            return event.get_messages() or []
        except Exception:
            return []

    @classmethod
    def _at_targets(cls, event):
        """提取消息中的真实 @ 对象，并过滤机器人自身；不要依赖命令文本与 At 段的相对位置。"""
        found = []

        def add_target(value):
            if value is None:
                return
            value = str(value).strip()
            if not value or value.lower() in ("all", "0", "none", "null"):
                return
            if value not in found:
                found.append(value)

        def seg_type(seg):
            typ = getattr(seg, "type", None)
            if isinstance(seg, dict):
                typ = seg.get("type", typ)
            return str(getattr(typ, "value", typ) or "").lower().split(".")[-1]

        def seg_data(seg):
            data = getattr(seg, "data", None)
            if isinstance(seg, dict):
                data = seg.get("data", data)
            return data

        def inspect_at(seg):
            typ = seg_type(seg)
            # AstrBot 不同版本/适配器可能暴露 At、at 或 message_component_type.at。
            if typ != "at" and not typ.endswith("_at"):
                return
            data = seg_data(seg)
            if isinstance(data, dict):
                for key in ("qq", "user_id", "id", "target"):
                    if data.get(key) is not None:
                        add_target(data.get(key))
                        return
            elif data is not None:
                add_target(data)
            else:
                for key in ("qq", "user_id", "id", "target"):
                    value = getattr(seg, key, None)
                    if value is not None:
                        add_target(value)
                        return

        # 先拿机器人自己的 ID；前置唤醒 @ 必须被过滤，而不是作为首个目标。
        bot_ids = set()
        for getter_name in ("get_self_id", "get_bot_id"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    value = getter()
                    if value is not None:
                        bot_ids.add(str(value))
                except Exception:
                    pass
        bot = getattr(event, "bot", None)
        for attr in ("self_id", "id", "uin"):
            value = getattr(bot, attr, None) if bot is not None else None
            if value is not None:
                bot_ids.add(str(value))
        try:
            platform = event.get_platform()
            for attr in ("self_id", "id", "uin"):
                value = getattr(platform, attr, None) if platform is not None else None
                if value is not None:
                    bot_ids.add(str(value))
            getter = getattr(platform, "get_self_id", None)
            if callable(getter):
                value = getter()
                if value is not None:
                    bot_ids.add(str(value))
        except Exception:
            pass

        # 结构化消息链是首选：收集全部 At，之后统一排除机器人自身。
        try:
            for seg in list(event.get_messages() or []):
                inspect_at(seg)
        except Exception:
            logger.exception("[群友弹鼻屎] 读取结构化消息链中的 At 失败")

        # 兼容 OneBot CQ 原始消息及日志常见的 [At:123] 展示格式。
        import re
        raw_values = []
        msg_obj = getattr(event, "message_obj", None)
        for obj in (msg_obj, event):
            if obj is None:
                continue
            for attr in ("raw_message", "message_str"):
                try:
                    value = getattr(obj, attr, None)
                    if isinstance(value, str) and value:
                        raw_values.append(value)
                except Exception:
                    pass
        for value in raw_values:
            for match in re.finditer(r"\[CQ:at,\s*(?:qq|user_id)=(\d+)\]", value, re.I):
                add_target(match.group(1))
            for match in re.finditer(r"\[At:(\d+)\]", value, re.I):
                add_target(match.group(1))

        targets = [uid for uid in found if uid not in bot_ids]
        logger.info("[群友弹鼻屎] @解析: bot_ids=%s all_at=%s targets=%s", sorted(bot_ids), found, targets)
        return targets

    def _group(self, gid):
        return self.state.setdefault("groups", {}).setdefault(
            gid, {"users": {}, "stats": {}, "daily_hits": {}, "daily_date": datetime.now().strftime("%Y-%m-%d")}
        )

    def _user(self, gid, uid, name=None):
        users = self._group(gid)["users"]
        obj = users.setdefault(uid, {"count": 0, "stink": 0, "name": name or f"群友{uid[-4:]}"})
        if name:
            obj["name"] = name
        return obj

    def _record_hit(self, gid, target_id, target_name):
        g = self._group(gid)
        today = datetime.now().strftime("%Y-%m-%d")
        if g.get("daily_date") != today:
            g["daily_date"] = today
            g["daily_hits"] = {}
        hits = g.setdefault("daily_hits", {})
        hits[target_id] = int(hits.get(target_id, 0)) + 1
        self._user(gid, target_id, target_name)

    def _add_booger(self, gid, uid, name, amount=1):
        user = self._user(gid, uid, name)
        before = int(user.get("count", 0))
        user["count"] = min(100, before + amount)
        user["stink"] = min(100, int(user.get("stink", 0)) + random.randint(15, 25))
        return user, before

    @staticmethod
    def _result(event, text):
        return event.plain_result(text)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def _remember_group_session(self, event: AstrMessageEvent):
        """记录真实会话来源和当前 bot；定时任务不能自行拼接 UMO。"""
        gid = self._gid(event)
        if not gid:
            return
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._group_bots[gid] = bot
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            self._group_umos[gid] = str(umo)
            g = self._group(gid)
            if g.get("session_umo") != str(umo):
                g["session_umo"] = str(umo)
                self._save()

    @filter.command("弹鼻屎游戏")
    async def game_menu(self, event: AstrMessageEvent):
        self._last_bot = getattr(event, "bot", None)
        # 用真正的图片消息发送菜单，不再依赖 QQ 文本换行渲染。
        menu_path = Path(__file__).parent / "menu.png"
        if not menu_path.is_file():
            yield self._result(event, "菜单图片文件缺失，请重新安装完整插件 ZIP。")
            return
        yield event.chain_result([Image.fromFileSystem(str(menu_path))])

    async def _member_name(self, event, gid, target_id):
        """通过群成员资料解析 @ 目标昵称，失败时才使用 ID 后四位兜底。"""
        bot = getattr(event, "bot", None)
        group_value = int(gid) if str(gid).isdigit() else gid
        if bot is not None:
            for method_name in ("get_group_member_info", "get_group_member_profile"):
                method = getattr(bot, method_name, None)
                if callable(method):
                    for kwargs in (
                        {"group_id": group_value, "user_id": int(target_id) if str(target_id).isdigit() else target_id},
                        {"group_id": gid, "user_id": target_id},
                    ):
                        try:
                            result = method(**kwargs)
                            if hasattr(result, "__await__"):
                                result = await result
                            if isinstance(result, dict):
                                result = result.get("data", result.get("result", result))
                                if isinstance(result, dict):
                                    name = result.get("card") or result.get("nickname") or result.get("name")
                                    if name:
                                        return str(name)
                        except Exception:
                            pass
            action = getattr(bot, "call_action", None)
            if callable(action):
                for kwargs in (
                    {"action": "get_group_member_info", "group_id": group_value, "user_id": int(target_id) if str(target_id).isdigit() else target_id},
                    {"action": "get_group_member_info", "group_id": gid, "user_id": target_id},
                ):
                    try:
                        result = action(**kwargs)
                        if hasattr(result, "__await__"):
                            result = await result
                        if isinstance(result, dict):
                            result = result.get("data", result.get("result", result))
                            if isinstance(result, dict):
                                name = result.get("card") or result.get("nickname") or result.get("name")
                                if name:
                                    return str(name)
                    except Exception:
                        pass
        # 如果插件已经保存过该用户的群名，优先使用缓存。
        cached = self._group(gid).get("users", {}).get(str(target_id), {})
        cached_name = cached.get("name")
        if cached_name and not str(cached_name).startswith("群友"):
            return str(cached_name)
        return f"群友{str(target_id)[-4:]}"

    @filter.command("弹鼻屎")
    async def flick(self, event: AstrMessageEvent):
        gid, uid, name = self._gid(event), self._uid(event), self._name(event)
        if not gid:
            yield self._result(event, "🤧 这项运动必须在群里开展，私聊没有观众！")
            return

        targets = self._at_targets(event)
        now = time.time()
        key = f"{gid}:{uid}"
        last = float(self.state.setdefault("cooldowns", {}).get(key, 0))
        remain = self.cooldown - (now - last)
        if remain > 0:
            yield self._result(event, f"🫰 手指正在冷却，还要 {int(remain) + 1} 秒才能再次发射。")
            return

        g = self._group(gid)
        actor = self._user(gid, uid, name)
        stats = g.setdefault("stats", {}).setdefault(uid, {"flicks": 0, "hits": 0})
        stats["flicks"] = int(stats.get("flicks", 0)) + 1

        # 未 @ 任何人时，与 /随机弹鼻屎 完全相同：先随机抽取真实群友，不再弹空气。
        random_mode = not bool(targets)
        if random_mode:
            picked = await self._random_group_member(event, gid, uid)
            if not picked:
                yield self._result(event, "🎲 暂时拿不到群成员名单，也没有其他已记录的群友可抽选。请先让其他群友参与一次游戏，或检查 QQ 适配器的群成员列表接口。")
                return
            target_id, target_name = picked
        else:
            target_id = targets[0]
            if target_id == uid:
                yield self._result(event, "不能把自己作为指定目标哦。想随机抽其他群友，请直接发送 /弹鼻屎（不用 @ 人）。")
                return
            target_name = await self._member_name(event, gid, target_id)

        self.state["cooldowns"][key] = now
        target = self._user(gid, target_id, target_name)
        roll = random.random()
        if roll < 0.65:
            target, before = self._add_booger(gid, target_id, target_name)
            self._record_hit(gid, target_id, target_name)
            stats["hits"] = int(stats.get("hits", 0)) + 1
            if before >= 99:
                msg = f"💥💚 {name} 打出暴击！{target_name} 脸上已堆满 100 颗，荣获「鼻屎王」临时称号！恶臭值 {target['stink']}/100。"
            else:
                msg = random.choice([
                    f"🎯 精准命中！{name} 把鼻屎弹在 {target_name} 脸上。当前 {target['count']} 颗，恶臭值 {target['stink']}/100。",
                    f"💥 啪！{target_name} 中招，鼻屎稳稳落在脸上。群里响起了虚假的掌声。",
                    f"🫰 神之一弹！{name} 命中 {target_name}，建议直接报名鼻屎奥运会。当前 {target['count']} 颗。",
                ])
        elif roll < 0.85:
            msg = random.choice([
                f"💨 鼻屎擦着 {target_name} 的耳朵飞过，顺便给发型做了个造型。",
                f"🌬️ {name} 出手！{target_name} 触发闪避，鼻屎只留下了一阵难以描述的风。",
                f"😮 差一点！鼻屎在 {target_name} 面前表演了贴脸飞行，然后扬长而去。",
            ])
        elif roll < 0.95:
            actor, _ = self._add_booger(gid, uid, name)
            msg = f"🤦 反弹！{name} 的鼻屎被命运的后脑勺挡回，自己脸上新增一颗（当前 {actor['count']} 颗）。"
        else:
            msg = f"🎲 鼻屎发生弹道偏移，落入了群聊未知区域。全体成员默契地假装没看见。"
        self._save()
        yield self._result(event, msg)

    async def _random_group_member(self, event: AstrMessageEvent, gid: str, sender_id: str):
        """优先从平台获取真实群成员；失败时从本插件已见过的群成员中抽取。"""
        members = []
        bot = getattr(event, "bot", None)
        group_value = int(gid) if str(gid).isdigit() else gid

        # AstrBot/OneBot 不同版本暴露的群成员列表接口略有差异，逐个尝试。
        if bot is not None:
            methods = []
            direct = getattr(bot, "get_group_member_list", None)
            if callable(direct):
                methods.append((direct, [{"group_id": group_value}, {"group_id": gid}, {"group_id": group_value, "no_cache": False}]))
            call_action = getattr(bot, "call_action", None)
            if callable(call_action):
                methods.append((call_action, [
                    {"action": "get_group_member_list", "group_id": group_value},
                    {"action": "get_group_member_list", "group_id": gid},
                ]))
            api = getattr(bot, "api", None)
            api_call = getattr(api, "call_action", None) if api is not None else None
            if callable(api_call):
                methods.append((api_call, [
                    {"action": "get_group_member_list", "group_id": group_value},
                    {"action": "get_group_member_list", "group_id": gid},
                ]))
            for method, variants in methods:
                for kwargs in variants:
                    try:
                        result = method(**kwargs)
                        if hasattr(result, "__await__"):
                            result = await result
                        if isinstance(result, dict):
                            result = result.get("data", result.get("members", result.get("result", result)))
                        if isinstance(result, (list, tuple)):
                            members = list(result)
                            if members:
                                break
                    except Exception:
                        continue
                if members:
                    break

        candidates = {}
        for member in members:
            if isinstance(member, dict):
                mid = member.get("user_id", member.get("qq", member.get("id")))
                mname = member.get("card") or member.get("nickname") or member.get("name")
            else:
                mid = getattr(member, "user_id", getattr(member, "qq", getattr(member, "id", None)))
                mname = getattr(member, "card", None) or getattr(member, "nickname", None) or getattr(member, "name", None)
            if mid is not None and str(mid) != str(sender_id):
                candidates[str(mid)] = str(mname or f"群友{str(mid)[-4:]}")

        # API 不可用时使用已参与过游戏或曾被 @ 的群友，绝不把空气当目标。
        known = self._group(gid).get("users", {})
        for mid, info in known.items():
            if str(mid) != str(sender_id):
                candidates.setdefault(str(mid), str(info.get("name") or f"群友{str(mid)[-4:]}"))
        if not candidates:
            return None
        target_id = random.choice(list(candidates.keys()))
        return target_id, candidates[target_id]

    @filter.command("随机弹鼻屎")
    async def random_flick(self, event: AstrMessageEvent):
        """从真实群成员中随机抽取目标；没有可用成员时明确提示，不会假装命中空气。"""
        gid, uid, name = self._gid(event), self._uid(event), self._name(event)
        if not gid:
            yield self._result(event, "这个玩法只能在群里玩哦。")
            return
        key = f"{gid}:{uid}"
        now = time.time()
        remain = self.cooldown - (now - float(self.state.setdefault("cooldowns", {}).get(key, 0)))
        if remain > 0:
            yield self._result(event, f"🫰 手指冷却中，再等 {int(remain) + 1} 秒。")
            return

        picked = await self._random_group_member(event, gid, uid)
        if not picked:
            yield self._result(event, "🎲 暂时拿不到群成员名单，也没有其他已记录的群友可抽选。请先用 /弹鼻屎 @某人 和群友玩过一次，之后随机模式就能从已记录的群友中抽取。")
            return

        target_id, target_name = picked
        self.state["cooldowns"][key] = now
        self._user(gid, uid, name)
        target = self._user(gid, target_id, target_name)
        g = self._group(gid)
        stats = g.setdefault("stats", {}).setdefault(uid, {"flicks": 0, "hits": 0})
        stats["flicks"] = int(stats.get("flicks", 0)) + 1

        roll = random.random()
        if roll < 0.65:
            target, before = self._add_booger(gid, target_id, target_name)
            self._record_hit(gid, target_id, target_name)
            stats["hits"] = int(stats.get("hits", 0)) + 1
            if before >= 99:
                msg = f"🎲🎯 随机抽中 {target_name}！{name} 一发暴击，{target_name} 脸上堆满 100 颗，荣获「鼻屎王」！恶臭值 {target['stink']}/100。"
            else:
                msg = random.choice([
                    f"🎲🎯 命运抽中了 {target_name}！{name} 的鼻屎精准命中，对方脸上现在有 {target['count']} 颗。",
                    f"🎯 随机点名成功！{target_name} 躺枪，{name} 获得「群聊投射物」称号。当前 {target['count']} 颗。",
                    f"💥 群友抽奖结果：{target_name}！鼻屎正中目标，恶臭值升至 {target['stink']}/100。",
                ])
        elif roll < 0.85:
            msg = f"💨 随机抽中的目标是 {target_name}！但对方一个闪身，鼻屎擦着耳朵飞过，发型受到轻微惊吓。"
        elif roll < 0.95:
            actor, _ = self._add_booger(gid, uid, name)
            msg = f"🤦 随机目标本来是 {target_name}，没想到鼻屎中途反弹，最后糊回 {name} 自己脸上！当前 {actor['count']} 颗。"
        else:
            msg = f"🌀 随机目标锁定 {target_name}，但鼻屎被一阵神秘气流吹偏——目标躲过一劫，{name} 的投射技术遭到全群质疑。"
        self._save()
        yield self._result(event, msg)

    @filter.command("擦鼻屎")
    async def wipe(self, event: AstrMessageEvent):
        gid, uid = self._gid(event), self._uid(event)
        user = self._user(gid or "private", uid, self._name(event))
        if int(user.get("count", 0)) <= 0:
            yield self._result(event, "✨ 你脸上干干净净。今天的体面暂时保住了。")
            return
        roll = random.random()
        if roll < 0.70:
            user["count"] = max(0, int(user.get("count", 0)) - 1)
            user["stink"] = max(0, int(user.get("stink", 0)) - 10)
            msg = f"🧻 清理成功！剩余 {user['count']} 颗，恶臭值 {user['stink']}/100。恭喜恢复一点尊严。"
        elif roll < 0.90:
            msg = f"💨 擦歪了！一顿操作后鼻屎还牢牢粘着，仍剩 {user['count']} 颗。再试一次吧（冷静点）。"
        else:
            user["stink"] = min(100, int(user.get("stink", 0)) + 10)
            msg = f"🤢 越擦越糟！不仅没擦掉，恶臭值还 +10，当前 {user['stink']}/100。你的手法值得反思。"
        self._save()
        yield self._result(event, msg)

    @filter.command("帮擦")
    async def help_wipe(self, event: AstrMessageEvent):
        gid, uid, name = self._gid(event), self._uid(event), self._name(event)
        targets = self._at_targets(event)
        if not gid:
            yield self._result(event, "帮擦是群体运动，请回群里操作。")
            return
        if not targets:
            yield self._result(event, "请使用：/帮擦 @某人")
            return
        target_id = targets[0]
        if target_id == uid:
            yield self._result(event, "自己擦自己请用 /擦鼻屎，没必要假装有第三只手。")
            return
        target_name = await self._member_name(event, gid, target_id)
        target = self._user(gid, target_id, target_name)
        actor = self._user(gid, uid, name)
        if int(target.get("count", 0)) <= 0:
            yield self._result(event, f"✨ {target['name']} 干净得像没参加过这个游戏，你的帮助无处发挥。")
            return
        if random.random() < 0.70:
            target["count"] -= 1
            target["stink"] = max(0, int(target.get("stink", 0)) - 10)
            msg = f"🧻 {name} 成功帮 {target['name']} 擦掉一颗！这份友情将被永远记住（直到下次开黑）。"
        else:
            target["count"] -= 1
            actor["count"] = min(100, int(actor.get("count", 0)) + 1)
            actor["stink"] = min(100, int(actor.get("stink", 0)) + 10)
            msg = f"💀 帮擦现场发生事故！{name} 手一抖把鼻屎转移到了自己脸上。医药费请自行协商。"
        self._save()
        yield self._result(event, msg)

    @filter.command("鼻屎榜")
    async def leaderboard(self, event: AstrMessageEvent):
        gid = self._gid(event) or "private"
        g = self._group(gid)
        users, stats = g.get("users", {}), g.get("stats", {})
        if not users:
            yield self._result(event, "榜单暂时空空如也。使用 /弹鼻屎 或 /随机弹鼻屎 开始制造历史。")
            return
        messy = sorted(users.items(), key=lambda x: (int(x[1].get("count", 0)), int(x[1].get("stink", 0))), reverse=True)[:5]
        accurate = sorted(stats.items(), key=lambda x: (int(x[1].get("hits", 0)), int(x[1].get("flicks", 0))), reverse=True)[:5]
        lines = ["🏆 本群鼻屎榜", "【脸上最热闹】"]
        lines += [f"{i}. {u.get('name', '神秘群友')}：{u.get('count', 0)} 颗｜恶臭 {u.get('stink', 0)}/100" for i, (_, u) in enumerate(messy, 1)]
        lines.append("【弹得最准】")
        lines += [f"{i}. {users.get(uid, {}).get('name', '神秘群友')}：命中 {s.get('hits', 0)} 次 / 出手 {s.get('flicks', 0)} 次" for i, (uid, s) in enumerate(accurate, 1)]
        yield self._result(event, "\n".join(lines))

    @filter.command("鼻屎状态")
    async def status(self, event: AstrMessageEvent):
        gid, uid = self._gid(event) or "private", self._uid(event)
        user = self._user(gid, uid, self._name(event))
        yield self._result(event, f"🪞 鼻屎状态：{user.get('count', 0)}/100 颗；恶臭值 {user.get('stink', 0)}/100。")

    @filter.command("整点播报")
    async def hourly_toggle(self, event: AstrMessageEvent):
        """按群开关每小时整点随机弹射播报：/整点播报 开 或 /整点播报 关。"""
        gid = self._gid(event)
        self._last_bot = getattr(event, "bot", None)
        if not gid:
            yield self._result(event, "请在群聊中使用：/整点播报 开 或 /整点播报 关")
            return
        raw = str(getattr(event, "message_str", "") or "").strip()
        # 不同 AstrBot 版本 message_str 可能包含或不包含命令前缀，取最后一个词作为参数。
        arg = raw.split()[-1].strip().lower() if raw.split() else ""
        g = self._group(gid)
        if arg in ("开", "开启", "on", "打开", "启用"):
            g["hourly_enabled"] = True
            self._save()
            yield self._result(event, "⏰ 本群每小时整点随机弹射播报已开启。")
        elif arg in ("关", "关闭", "off", "停用"):
            g["hourly_enabled"] = False
            self._save()
            yield self._result(event, "🔕 本群每小时整点随机弹射播报已关闭。")
        else:
            enabled = g.get("hourly_enabled", True)
            yield self._result(event, f"本群每小时整点播报当前为：{'开启' if enabled else '关闭'}。用法：/整点播报 开 或 /整点播报 关")

    @filter.command("今日鼻屎王")
    async def daily_king(self, event: AstrMessageEvent):
        gid = self._gid(event) or "private"
        g = self._group(gid)
        today = datetime.now().strftime("%Y-%m-%d")
        hits = g.get("daily_hits", {}) if g.get("daily_date") == today else {}
        if not hits:
            yield self._result(event, "👑 今日鼻屎王暂时空缺——大家今天都还挺干净（或是弹得太菜）。")
            return
        uid, count = max(hits.items(), key=lambda x: int(x[1]))
        name = self._user(gid, uid).get("name", f"群友{uid[-4:]}")
        yield self._result(event, f"👑 今日鼻屎王：{name}！今天累计被精准命中 {count} 次。请发表获奖感言，并领取虚拟纸巾一包。")

    async def _announcement_loop(self):
        """每分钟检查一次：整点群内随机弹射、每天 23:00 公布鼻屎王。"""
        while True:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            # 每个小时只触发一次，插件重载后不会在同一小时重复触发。
            slot = now.strftime("%Y-%m-%d-%H")
            if now.minute == 0 and self._last_hourly_slot != slot:
                self._last_hourly_slot = slot
                if self._is_quiet_time(now):
                    logger.info("当前处于免打扰时段，跳过整点播报：%s", now.strftime("%H:%M"))
                else:
                    for gid in list(self.state.get("groups", {}).keys()):
                        if not gid or gid == "private":
                            continue
                        try:
                            await self._hourly_random_flick(gid)
                        except Exception as exc:
                            logger.exception("整点随机弹射失败，群 %s：%s", gid, exc)
                            try:
                                await self._send_group(gid, f"⏰【整点随机弹射】本次执行出错：{type(exc).__name__}。请查看 AstrBot 日志。")
                            except Exception as send_exc:
                                logger.warning("整点错误提示也未能发送，群 %s：%s", gid, send_exc)

            # 每天 23:00 公布今日鼻屎王；免打扰时段内同样跳过。
            if now.hour == 23 and now.minute == 0 and self._last_daily_date != today:
                self._last_daily_date = today
                if self._is_quiet_time(now):
                    logger.info("当前处于免打扰时段，跳过每日鼻屎王播报")
                else:
                    for gid in list(self.state.get("groups", {}).keys()):
                        if not gid or gid == "private":
                            continue
                        try:
                            g = self._group(gid)
                            hits = g.get("daily_hits", {}) if g.get("daily_date") == today else {}
                            if hits:
                                uid, count = max(hits.items(), key=lambda x: int(x[1]))
                                name = self._user(gid, uid).get("name", f"群友{str(uid)[-4:]}")
                                message = f"🌙【今日鼻屎王颁奖典礼】恭喜 {name}！今天被精准命中 {count} 次，荣获今日鼻屎王！奖品：全群敬而远之三秒钟。"
                            else:
                                message = "🌙【今日鼻屎王颁奖典礼】今日无人中选！本群保持了难得的卫生纪录，明天继续争夺（？）"
                            await self._send_group(gid, message)
                        except Exception as exc:
                            logger.warning("鼻屎王播报失败，群 %s：%s", gid, exc)
                    self._save()

            await asyncio.sleep(max(1, 60 - datetime.now().second))

    def _is_quiet_time(self, now=None):
        """按插件配置判断是否处于免打扰时段，支持跨午夜时间段。"""
        if not self._config_bool("enable_quiet_hours", True):
            return False
        now = now or datetime.now()
        try:
            start_text = str(self.config.get("quiet_start", "00:00")).strip()
            end_text = str(self.config.get("quiet_end", "08:00")).strip()
            start_h, start_m = (int(x) for x in start_text.split(":", 1))
            end_h, end_m = (int(x) for x in end_text.split(":", 1))
            if not (0 <= start_h <= 24 and 0 <= end_h <= 24 and 0 <= start_m <= 59 and 0 <= end_m <= 59):
                raise ValueError("时间必须是 HH:MM")
            if start_h == 24 and start_m != 0 or end_h == 24 and end_m != 0:
                raise ValueError("24 点只允许填写 24:00")
            if start_h == 24:
                start_h = 0
            if end_h == 24:
                end_h = 0
            current = now.hour * 60 + now.minute
            start = start_h * 60 + start_m
            end = end_h * 60 + end_m
            if start == end:
                return True
            if start < end:
                return start <= current < end
            return current >= start or current < end
        except Exception as exc:
            logger.warning("免打扰时间配置无效（需使用 HH:MM 格式），本次不启用免打扰判断：%s", exc)
            return False

    def _config_bool(self, key, default=True):
        """兼容 AstrBot 配置对象及普通 dict，安全读取布尔配置。"""
        try:
            value = self.config.get(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "开启", "开")
            return bool(value)
        except Exception:
            return default

    async def _hourly_random_flick(self, gid):
        """从平台返回的全体群成员中抽选一人；整点事件固定命中并播报。"""
        g = self._group(gid)
        # 管理面板是全局总开关；关闭时优先阻止所有群的整点播报。
        panel_enabled = self._config_bool("enable_hourly_broadcast", True)
        if not panel_enabled:
            logger.info("插件配置已关闭整点播报，跳过群 %s", gid)
            return
        if g.get("hourly_enabled", True) is False:
            logger.info("群 %s 已关闭整点播报，跳过", gid)
            return

        # sender_id 使用不会与正常 QQ 号冲突的哨兵值，因此不会刻意排除任何群成员。
        selected = await self._random_group_member_for_announcement(gid)
        if not selected:
            logger.warning("群 %s 获取不到全体群成员名单，无法保证全群随机抽选", gid)
            await self._send_group(gid, "⏰【整点随机弹射】本次未能获取群成员名单，播报未完成。请检查 QQ 适配器的群成员列表接口。")
            return

        target_id, target_name = selected
        target, before = self._add_booger(gid, target_id, target_name)
        self._record_hit(gid, target_id, target_name)
        message = (f"⏰🎯【整点随机弹射】本小时全群随机抽中了 {target_name}！必中成功！"
                   f"新增 1 颗鼻屎，现在脸上 {target['count']}/100 颗，恶臭值 {target['stink']}/100。")
        self._save()
        await self._send_group(gid, message)

    async def _random_group_member_for_announcement(self, gid):
        """获取平台全体群成员；对接口调用设置超时，失败时交由整点流程发送提示。"""
        bot = self._group_bots.get(str(gid))
        if bot is None:
            logger.warning("群 %s 尚未缓存到最近的群消息 bot 对象", gid)
            return None

        group_value = int(gid) if str(gid).isdigit() else gid
        members = None
        errors = []
        api = getattr(bot, "api", None)
        api_call = getattr(api, "call_action", None) if api is not None else None
        bot_call = getattr(bot, "call_action", None)

        # aiocqhttp 常见签名是 call_action(action, **params)；兼容 no_cache 参数差异。
        for caller, label in ((api_call, "bot.api.call_action"), (bot_call, "bot.call_action")):
            if not callable(caller):
                continue
            for kwargs in ({"group_id": group_value, "no_cache": True}, {"group_id": group_value}):
                try:
                    result = caller("get_group_member_list", **kwargs)
                    if hasattr(result, "__await__"):
                        result = await asyncio.wait_for(result, timeout=8)
                    if isinstance(result, dict):
                        result = result.get("data", result.get("members", result.get("result")))
                    if isinstance(result, (list, tuple)) and result:
                        members = list(result)
                        logger.info("群 %s 通过 %s 获取到 %s 名群成员", gid, label, len(members))
                        break
                    errors.append(f"{label}: 返回类型/成员列表为空 ({type(result).__name__})")
                except asyncio.TimeoutError:
                    errors.append(f"{label}: 调用超时")
                except Exception as exc:
                    errors.append(f"{label}: {type(exc).__name__}: {exc}")
            if members:
                break

        if not members:
            logger.warning("群 %s 获取群成员列表失败：%s", gid, "；".join(errors) or "没有可用的 API")
            return None

        candidates = []
        for member in members:
            if isinstance(member, dict):
                mid = member.get("user_id", member.get("qq", member.get("id")))
                name = member.get("card") or member.get("nickname") or member.get("name")
            else:
                mid = getattr(member, "user_id", getattr(member, "qq", getattr(member, "id", None)))
                name = getattr(member, "card", None) or getattr(member, "nickname", None) or getattr(member, "name", None)
            if mid is not None:
                candidates.append((str(mid), str(name or f"群友{str(mid)[-4:]}")))

        if not candidates:
            logger.warning("群 %s 的成员 API 返回了数据，但未找到任何 user_id", gid)
            return None
        return random.choice(candidates)

    async def _send_group(self, gid, message, target_id=None):
        """用事件中记录的真实 UMO 主动发送；需要时插入真正的 At 消息段。"""
        gid = str(gid)
        umo = self._group_umos.get(gid) or self._group(gid).get("session_umo")
        if not umo:
            raise RuntimeError("尚未记录本群真实 unified_msg_origin；请先让群内产生一条消息后再等待整点任务")
        api = getattr(self.context, "send_message", None)
        if not callable(api):
            raise RuntimeError("当前 AstrBot Context 没有 send_message API")
        chain = MessageChain()
        if target_id is not None:
            # 先添加真正的 At 组件，再添加正文，不能只把昵称写进纯文本。
            try:
                chain.chain.append(At(qq=int(target_id) if str(target_id).isdigit() else str(target_id)))
            except Exception:
                chain.chain.append(At(qq=str(target_id)))
            chain.chain.append(Plain(text=" "))
        chain.message(str(message))
        result = api(umo, chain)
        if hasattr(result, "__await__"):
            await result

    async def initialize(self):
        try:
            self._scheduler_task = asyncio.create_task(self._announcement_loop())
            logger.info("群友弹鼻屎定时任务已启动：每小时整点随机弹射，每天 23:00 公布鼻屎王")
        except Exception as exc:
            self._scheduler_task = None
            logger.exception("群友弹鼻屎定时任务启动失败：%s", exc)

    async def terminate(self):
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
