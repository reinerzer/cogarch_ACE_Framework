import json
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import ace.l3_agent_prompts as prompts
from ace.ace_layer import AceLayer
from ace.action_enabled_llm import ActionEnabledLLM
from ace.l2_global_strategy import L2GlobalStrategyLayer
from ace.layer_status import LayerStatus
from ace.types import ChatMessage, Memory, stringify_chat_message, stringify_chat_history
from channels.communication_channel import CommunicationChannel
from llm.gpt import GPT, GptMessage
from memory.weaviate_memory_manager import WeaviateMemoryManager

chat_history_length_short = 3

chat_history_length = 10

max_memories_to_include = 5


class L3AgentLayer(AceLayer):
    def __init__(self, llm: GPT, model, memory_manager: WeaviateMemoryManager,
                 l2_global_strategy_layer: L2GlobalStrategyLayer):
        super().__init__("3")
        self.llm = llm
        self.model = model
        self.scheduler = AsyncIOScheduler()
        self.scheduler.start()
        self.memory_manager = memory_manager
        self.action_enabled_llm = ActionEnabledLLM(llm, model, self.scheduler, memory_manager, l2_global_strategy_layer)

    async def process_incoming_user_message(self, communication_channel: CommunicationChannel):
        # Early out if I don't need to act, for example if I overheard a message that wasn't directed at me
        if not await self.should_act(communication_channel):
            return

        chat_history: [ChatMessage] = await communication_channel.get_message_history(chat_history_length)
        if not chat_history:
            print("Warning: process_incoming_user_message was called with no chat history. That's weird. Ignoring.")
            return
        last_chat_message = chat_history[-1]
        user_name = last_chat_message['sender']

        memories: [Memory] = self.memory_manager.find_relevant_memories(
            stringify_chat_message(last_chat_message),
            max_memories_to_include
        )

        print("Found memories:\n" + json.dumps(memories, indent=2))
        system_message = self.create_system_message()

        memories_if_any = ""
        if memories:
            memories_string = "\n".join(f"- <{memory['time_utc']}>: {memory['content']}" for memory in memories)
            memories_if_any = prompts.memories.replace("[memories]", memories_string)

        user_message = (
            prompts.act_on_user_input
            .replace("[communication_channel]", communication_channel.describe())
            .replace("[memories_if_any]", memories_if_any)
            .replace("[chat_history]", stringify_chat_history(chat_history))
        )
        llm_messages: [GptMessage] = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message}
        ]
        print("System prompt: " + system_message)
        print("User prompt: " + user_message)
        await self.action_enabled_llm.talk_to_llm_and_execute_actions(communication_channel, user_name, llm_messages)

    def create_system_message(self):
        current_time_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        system_message = f"""
                {prompts.self_identity}
                {prompts.personality}
                {prompts.knowledge.replace("[current_time_utc]", current_time_utc)}
                {prompts.media_replacement}
                {prompts.actions}
            """
        return system_message

    async def should_act(self, communication_channel: CommunicationChannel):
        """
        Ask the LLM whether this is a message that we should act upon.
        This is a cheaper request than asking the LLM to generate a response,
        allows us to early-out for unrelated messages.
        """

        message_history: [ChatMessage] = await communication_channel.get_message_history(
            chat_history_length_short
        )

        prompt = prompts.decide_whether_to_respond_prompt.format(
            messages=stringify_chat_history(message_history)
        )

        print(f"Prompt to determine if we should respond:\n {prompt}")
        await self.set_status(LayerStatus.INFERRING)
        try:
            response = await self.llm.create_conversation_completion(
                self.model,
                [{"role": "user", "name": "user", "content": prompt}]
            )
            response_content = response['content'].strip().lower()

            print(f"Response to prompt: {response_content}")

            return response_content.startswith("yes")
        finally:
            await self.set_status(LayerStatus.IDLE)
