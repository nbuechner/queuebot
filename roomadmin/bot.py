from __future__ import annotations
import json, os, re, requests, asyncio, traceback, logging
from launchpadlib.launchpad import Launchpad
from pathlib import Path
from time import time
from typing import Type, Tuple
from urllib.parse import urlparse, unquote
from maubot import Plugin, MessageEvent
from maubot.handlers import command, event
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from mautrix.util import background_task
from mautrix.types import (
    EventType,
    MemberStateEventContent,
    PowerLevelStateEventContent,
    RoomID,
    RoomAlias,
    StateEvent,
    UserID,
    Membership
)
from .floodprotection import FloodProtection

roomadmin_change_level = EventType.find("com.ubuntu.roomadmin", t_class=EventType.Class.STATE)

class Config(BaseProxyConfig):
  def do_update(self, helper: ConfigUpdateHelper) -> None:
    helper.copy("whitelist")
    helper.copy("rooms")
    helper.copy("update_interval")

class Roomadmin(Plugin):
  reminder_loop_task: asyncio.Future
  room_ids = []
  room_mapping = {}
  power_level_cache: dict[RoomID, tuple[int, PowerLevelStateEventContent]]

  async def start(self) -> None:
    self.config.load_and_update()
    self.flood_protection = FloodProtection()
    self.power_level_cache = {}
    logger = logging.getLogger(self.id)
    logger.setLevel(logging.DEBUG)
    self.log = logger
    if await self.resolve_room_aliases():
        self.poll_task = asyncio.create_task(self.poll_sync())
        self.log.info("Roomadmin started")

  async def stop(self) -> None:
    await super().stop()
    self.poll_task.cancel()

  async def get_room_members(self, room_id):
    members = set()
    joined_members = await self.client.get_joined_members(room_id)
    if joined_members:
        for user_id in joined_members.keys():
            members.add(user_id)
    return set(members)

        
  @event.on(EventType.ROOM_MEMBER)
  async def handle_member_event(self, evt: StateEvent) -> None:
    if evt.content.membership == Membership.JOIN:
        user_id = evt.state_key
        room_id = evt.room_id
        joined_rooms = await self.client.get_joined_rooms()
        if room_id not in self.config["rooms"]:
            return False
        self.check_member_level(room_id, self.config["rooms"][room_id]["launchpad_groups"], user_id)

  async def get_power_levels(self, room_id: RoomID) -> PowerLevelStateEventContent:
        try:
            expiry, levels = self.power_level_cache[room_id]
            if expiry < int(time()):
                return levels
        except KeyError:
            pass
        levels = await self.client.get_state_event(room_id, EventType.ROOM_POWER_LEVELS)
        now = int(time())
        self.power_level_cache[room_id] = (now + 5 * 60, levels)
        return levels

  async def can_manage(self, evt: MessageEvent) -> bool:
        if evt.sender in self.config["whitelist"]:
            return True
        levels = await self.get_power_levels(evt.room_id)
        user_level = levels.get_user_level(evt.sender)
        state_level = levels.get_event_level(roomadmin_change_level)
        if not isinstance(state_level, int):
            state_level = 100
        if user_level < state_level:
            return False
        return True

  async def get_matrix_socials(self, mxid):
    try:
        if mxid.startswith('@') and ':' in mxid:
            profile_id = mxid.split(':')[0][1:]
            url = 'http://127.0.0.1:8000/launchpad/api/people/' + profile_id + '/socials/matrix'
            resp = await self.http.get(url)
            if resp.status == 200:
                data = await resp.json()
                return data
    except Exception as e:
        print(e)
        return False
    return False
  async def is_mxid_in_any_launchpad_group(self, mxid, groups):
        if mxid == '':
            return False
        for group in groups:
            gmembers = await self.get_launchpad_group_members(group)
            if gmembers:
                if mxid in gmembers['mxids']:
                    return group
        return False

  async def set_user_power_level(self, room_id, mxid, level):
    try:
            # Fetch the current power levels
            current_power_levels = await self.get_power_levels(room_id)
            # Update the power level for the specified user
            current_power_levels["users"][mxid] = level
            await self.client.send_state_event(
                room_id,
                "m.room.power_levels",
                current_power_levels,
                ""
            )
            return True
    except Exception as e:
            print(e)
            return False

  async def handle_sync(self, room_id, launchpad_groups):
        if not room_id in self.config["rooms"]:
            return False
        self.config["rooms"][room_id].setdefault("launchpad_groups", [])
        self.config["rooms"][room_id].setdefault("remove_permissions", [])
        self.config["rooms"][room_id].setdefault("enabled", 'yes')
        self.config["rooms"][room_id].setdefault("use_socials", 'no')
        if self.config["rooms"][room_id]["enabled"] != "yes":
            return False
        use_socials = self.config["rooms"][room_id]["use_socials"]
        room_members = await self.get_room_members(room_id)
        if room_members:
            room_members = set(room_members)
            for member in room_members:
                if member.endswith(':ubuntu.com'):
                    if use_socials == "yes":
                            social_ids = await self.get_matrix_socials(member)
                            if social_ids:
                                for social_id in social_ids:
                                    if not social_id.endswith(':ubuntu.com'):
                                        check = await self.check_member_level(room_id, launchpad_groups, member, social_id)
                                    else:
                                       check = await self.check_member_level(room_id, launchpad_groups, member)
                    else:
                        check = await self.check_member_level(room_id, launchpad_groups, member)

  
  async def check_member_level(self, room_id, launchpad_groups, member, social_id = ''):
    joined_rooms = await self.client.get_joined_rooms()
    # Check if the specified room ID is in the list of joined rooms
    try:
        if not room_id in joined_rooms:
            return False
    except Exception as e:
        print(f"Failed to check room membership: {e}")

    self.config["rooms"][room_id].setdefault("launchpad_groups", [])
    self.config["rooms"][room_id].setdefault("remove_permissions", 'yes')
    self.config["rooms"][room_id].setdefault("enabled", 'yes')
    levels = await self.get_power_levels(room_id)
    member_level = self.config["rooms"][room_id]["member_level"]
    nomember_level = self.config["rooms"][room_id]["nomember_level"]
    is_in_group = await self.is_mxid_in_any_launchpad_group(member, launchpad_groups)
    remove_permissions = self.config["rooms"][room_id]["remove_permissions"]
    if social_id != '':
        member = social_id
    user_level = levels.get_user_level(member)
    if user_level >= 100 or member == self.client.mxid:
        return False

    if is_in_group == False and remove_permissions == 'yes' and user_level != nomember_level:
        msg = "User " + member + " is not in any of the room's launchpad groups, has a power level of " + str(user_level) + " and should have a level of " + str(nomember_level)
        #await self.client.send_notice(room_id, msg)
        await self.set_user_power_level(room_id, member, nomember_level)
    elif user_level > member_level and remove_permissions == 'yes' and is_in_group != False:
        msg = "User " + member + " in group " + str(is_in_group) + " has a power level of " + str(user_level) + " and should have a lower group level of " + str(member_level)
        #await self.client.send_notice(room_id, msg)
        await self.set_user_power_level(room_id, member, member_level)
    elif user_level < member_level and is_in_group != False:
        msg = "User " + str(member) + " in group " + str(is_in_group) + " has a power level of " + str(user_level) + " and should have a higher group level of " + str(member_level)
        #await self.client.send_notice(room_id, msg)
        await self.set_user_power_level(room_id, member, member_level)

  async def get_launchpad_group_members(self, group_name):
    url = 'http://127.0.0.1:8000/launchpad/api/groups/members/' + str(group_name)
    try:
        resp = await self.http.get(url)
        if resp.status == 200:
            data = await resp.json()
            return data
    except Exception as e:
        print(e)
        return False
    return False

  @command.new(name="lpsync", require_subcommand=False)
  async def roomadmin(self, evt: MessageEvent) -> None:
        self.config["rooms"][room_id].setdefault("launchpad_groups", [])
        self.config["rooms"][room_id].setdefault("remove_permissions", [])
        self.config["rooms"][room_id].setdefault("enabled", 'yes')
        enabled = self.config["rooms"][room_id]["enabled"]
        launchpad_groups = self.config["rooms"][evt.room_id]["launchpad_groups"]
        if enabled != "yes":
            return False
        if not await self.can_manage(evt) and self.flood_protection.flood_check(evt.sender):
            await evt.respond("You don't have the permission to use this command.")
            return False
        await evt.respond("Invalid argument. Example: !roomadmin sync")
        if not await self.can_manage(evt) and self.flood_protection.flood_check(evt.sender):
            await evt.respond("You don't have the permission to use this command.")
            return False     

  async def resolve_room_alias_to_id(self, room_alias: str):
        try:
            room_id = await self.client.resolve_room_alias(RoomAlias(room_alias))
            return room_id
        except Exception as e:
            self.log.error(f"Error resolving room alias {room_alias}: {e}")
            self.log.debug(traceback.format_exc())
            return None

  async def resolve_room_aliases(self):
    self.room_ids = []
    self.room_mapping = {}
    for room_alias in self.config["rooms"]:
        if room_alias.startswith("#"):
            if room_id_obj := await self.resolve_room_alias_to_id(room_alias):
                room_id = str(room_id_obj.room_id)
                self.room_ids.append(room_id)
                self.room_mapping[room_id] = room_alias
                self.log.info("Added room " + room_alias + " with id " + room_id)
        elif room_alias.startswith("!"):
            self.room_ids.append(room_alias)
            self.room_mapping[room_alias] = room_alias
            self.log.info("Added room id " + room_alias)
        else:
            self.log.debug("Error addming room " + room_alias)
    return True
    
  def check_access_sender(self, sender):
      if sender in self.config["whitelist"]:
         return True
      return False

  async def poll_sync(self) -> None:
        try:
            await self._poll_sync()
        except asyncio.CancelledError:
            self.log.info("Sync stopped")
        except Exception:
            self.log.exception("Fatal error while syncing")
  async def _poll_sync(self) -> None:
        self.log.info("Syncing started")
        while True:
            try:
                await self._sync_once()
            except asyncio.CancelledError:
                self.log.info("Syncing stopped")
            except Exception:
                self.log.exception("Error while syncing")
            self.log.debug("Sleeping " + str(self.config["update_interval"] * 60) + " seconds")
            await asyncio.sleep(self.config["update_interval"] * 60)
  async def _sync_once(self) -> None:
    try:
        for room_id in self.config["rooms"]:
            self.config["rooms"][room_id].setdefault("launchpad_groups", [])
            self.config["rooms"][room_id].setdefault("remove_permissions", [])
            self.config["rooms"][room_id].setdefault("enabled", 'yes')
            enabled = self.config["rooms"][room_id]["enabled"]
            launchpad_groups = self.config["rooms"][room_id]["launchpad_groups"]
            try:
                sync = await self.handle_sync(room_id, launchpad_groups)
                if sync:
                    return True
            except Exception as e:
                self.log.debug(f"Error fetching members: {e}")
                self.log.debug(traceback.format_exc())
                pass
    except Exception as e:
        self.log.debug(f"Error syncing: {e}")
        self.log.debug(traceback.format_exc())
        pass

  @classmethod
  def get_config_class(cls) -> Type[BaseProxyConfig]:
    return Config
