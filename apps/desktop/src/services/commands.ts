/**
 * Command Registry
 *
 * Central registry of all IDE commands with keybindings.
 * Used by the CommandPalette, TitleBar buttons, and global hotkeys.
 * Single source of truth — every action is reachable via palette.
 */

export interface Command {
  id: string;
  title: string;
  category: string;
  keybinding?: string;
  icon?: string;
  run: () => void | Promise<void>;
}

class CommandRegistry {
  private commands = new Map<string, Command>();

  register(command: Command): void {
    this.commands.set(command.id, command);
  }

  registerAll(commands: Command[]): void {
    for (const cmd of commands) {
      this.commands.set(cmd.id, cmd);
    }
  }

  get(id: string): Command | undefined {
    return this.commands.get(id);
  }

  getAll(): Command[] {
    return Array.from(this.commands.values());
  }

  getByCategory(category: string): Command[] {
    return this.getAll().filter((c) => c.category === category);
  }

  /** Search commands by title (fuzzy, case-insensitive). */
  search(query: string): Command[] {
    const lower = query.toLowerCase();
    return this.getAll().filter(
      (c) =>
        c.title.toLowerCase().includes(lower) ||
        c.id.toLowerCase().includes(lower),
    );
  }

  /** Get all keybindings for global hotkey registration. */
  getKeybindings(): Array<{ keybinding: string; commandId: string }> {
    return this.getAll()
      .filter((c) => c.keybinding)
      .map((c) => ({ keybinding: c.keybinding!, commandId: c.id }));
  }
}

/** Singleton command registry. */
export const commandRegistry = new CommandRegistry();
