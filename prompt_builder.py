#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from typing import List, Dict, Any

class CacheAlignedPromptBuilder:
    def __init__(self, block_token_limit: int = 2000):
        self.block_token_limit = block_token_limit
        
    def _estimate_tokens(self, text: str) -> int:
        # Standard character-to-token ratio heuristic (4 chars ~ 1 token)
        return len(text) // 4

    def build_prompt_payload(
        self, 
        system_prompt: str, 
        tool_schemas: List[Dict[str, Any]], 
        user_profile: Dict[str, Any],
        raw_history: List[Dict[str, str]],
        dynamic_memories: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Segments prompts to align with Claude prompt caching standards.
        Zone 1 (Cached Prefix): Persona, tools, profile, and sealed conversation blocks.
        Zone 2 (Uncached Suffix): Recent conversation turns, retrieved notes, active user prompt.
        """
        # 1. Compile System Prompt and Tool Schemas
        base_static_text = (
            f"Persona & Instructions:\n{system_prompt}\n\n"
            f"Tool Schemas:\n{json.dumps(tool_schemas, indent=2)}\n\n"
            f"User Profile:\n{json.dumps(user_profile, indent=2)}"
        )
        
        # 2. Segment history into Sealed Blocks (stable across turns)
        # Keep the latest 3 turns in a dynamic uncached buffer
        buffer_turns = 3
        sealable_history = raw_history[:-buffer_turns] if len(raw_history) > buffer_turns else []
        active_buffer_history = raw_history[-buffer_turns:] if len(raw_history) > buffer_turns else raw_history
        
        sealed_history_blocks: List[List[Dict[str, str]]] = []
        current_block: List[Dict[str, str]] = []
        current_block_tokens = 0
        
        for turn in sealable_history:
            turn_tokens = self._estimate_tokens(turn.get("content", ""))
            if current_block_tokens + turn_tokens > self.block_token_limit:
                if current_block:
                    sealed_history_blocks.append(current_block)
                current_block = [turn]
                current_block_tokens = turn_tokens
            else:
                current_block.append(turn)
                current_block_tokens += turn_tokens
        if current_block:
            sealed_history_blocks.append(current_block)
            
        # Assemble message payload
        messages = []
        
        # First message: Base system core (Cached Breakpoint 1)
        messages.append({
            "role": "user",
            "content": base_static_text,
            "cache_control": {"type": "ephemeral"}
        })
        
        # Sealed blocks: Large, stable history chunks (Cached Breakpoint 2)
        for i, block in enumerate(sealed_history_blocks):
            content_str = "\n".join([f"{t['role'].upper()}: {t['content']}" for t in block])
            msg = {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"[Sealed History Block {i}]\n{content_str}"
            }
            # Cache the last sealed block to keep the whole history prefix hot
            if i == len(sealed_history_blocks) - 1:
                msg["cache_control"] = {"type": "ephemeral"}
            messages.append(msg)
            
        # Active history buffer (Dynamic uncached zone)
        for turn in active_buffer_history:
            messages.append({
                "role": turn["role"],
                "content": turn["content"]
            })
            
        # Contextual memories (Dynamic uncached suffix)
        if dynamic_memories:
            memories_str = "\n".join([f"- {m}" for m in dynamic_memories])
            messages.append({
                "role": "user",
                "content": f"[Retrieved Contextual Memories]\n{memories_str}\n\nApply the above rules and context to the active task."
            })
            
        return messages

if __name__ == '__main__':
    print("Prompt Builder module initialized.")
