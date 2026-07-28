/**
 * useAgent Hook
 *
 * Manages the agent conversation loop: send messages, process tool calls,
 * stream responses to the chat panel. Wired to the real AgentService which
 * bootstraps the full stack (Memory + LLM + ContextBuilder + Tools).
 */

import { useCallback, useRef, useEffect } from "react";
import { useAppStore, type ChatMessage } from "../stores/appStore";
import { agentService } from "../services/agentService";
import { nanoid } from "nanoid";
import type { TurnEvent } from "@ami/shared";

export function useAgent(sessionId = "default") {
  const {
    chatMessages,
    addChatMessage,
    addChatMessageToSession,
    updateChatMessage,
    setStreaming,
    isStreaming,
    addRecentMemory,
  } = useAppStore();

  const abortRef = useRef(false);

  /**
   * Initialize the agent service on first use.
   */
  useEffect(() => {
    if (!agentService.isInitialized) {
      agentService.initialize().catch((err) => {
        console.error("Failed to initialize agent service:", err);
      });
    }
  }, []);

  /**
   * Send a user message and process the agent's response.
   * Triggers the full ConversationLoop: context build → LLM → tool calls → memory.
   */
  const sendMessage = useCallback(
    async (content: string) => {
      if (isStreaming) return;

      // Try to initialize if not already done
      if (!agentService.isInitialized) {
        try {
          await agentService.initialize();
        } catch (err) {
          const userMsg: ChatMessage = {
            id: nanoid(),
            role: "user",
            content,
            timestamp: Date.now(),
          };
          addChatMessage(userMsg);
          const assistantId = nanoid();
          addChatMessage({
            id: assistantId,
            role: "assistant",
            content: `Failed to initialize agent: ${err instanceof Error ? err.message : String(err)}`,
            timestamp: Date.now(),
          });
          return;
        }
      }

      // Add user message
      const userMsg: ChatMessage = {
        id: nanoid(),
        role: "user",
        content,
        timestamp: Date.now(),
      };
      if (sessionId === "default") {
        addChatMessage(userMsg);
      } else {
        addChatMessageToSession(sessionId, userMsg);
      }

      // Create assistant placeholder
      const assistantId = nanoid();
      const assistantMsg: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        timestamp: Date.now(),
      };
      if (sessionId === "default") {
        addChatMessage(assistantMsg);
      } else {
        addChatMessageToSession(sessionId, assistantMsg);
      }

      setStreaming(true);
      abortRef.current = false;

      try {
        let accumulated = "";
        const toolCalls: Array<{ name: string; args: Record<string, unknown> }> = [];

        // Stream turn events from the conversation loop
        for await (const event of agentService.sendMessage(content, sessionId)) {
          if (abortRef.current) break;

          switch (event.type) {
            case "text":
              accumulated += event.text;
              updateChatMessage(assistantId, { content: accumulated });
              break;

            case "tool_call":
              toolCalls.push({ name: event.toolName, args: event.args });
              accumulated += `\n\n**Calling tool:** \`${event.toolName}\``;
              updateChatMessage(assistantId, { content: accumulated });
              break;

            case "tool_result":
              accumulated += `\n> ${event.result.preview}`;
              updateChatMessage(assistantId, { content: accumulated });
              break;

            case "error":
              accumulated += `\n\n**Error:** ${event.error}`;
              updateChatMessage(assistantId, { content: accumulated });
              break;

            case "done":
              break;
          }
        }
      } catch (err) {
        updateChatMessage(assistantId, {
          content: `Error: ${err instanceof Error ? err.message : "Unknown error"}`,
        });
      } finally {
        setStreaming(false);
      }
    },
    [addChatMessage, updateChatMessage, setStreaming, isStreaming, addRecentMemory],
  );

  /**
   * Abort the current response.
   */
  const abort = useCallback(() => {
    abortRef.current = true;
    setStreaming(false);
  }, [setStreaming]);

  /**
   * Clear the chat history.
   */
  const clearChat = useCallback(() => {
    useAppStore.getState().clearChat();
  }, []);

  return {
    sendMessage: (content: string) => sendMessage(content),
    abort,
    clearChat: () => sessionId === "default" ? useAppStore.getState().clearChat() : useAppStore.getState().deleteChatSession(sessionId),
    isStreaming,
    isInitialized: agentService.isInitialized,
    messages: chatMessages,
  };
}
