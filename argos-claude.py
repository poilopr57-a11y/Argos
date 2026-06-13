#!/usr/bin/env python3
"""
argos-claude.py — CLI для работы с Claude Code Templates в ARGOS

Использование:
    argos-claude search <query>              # Поиск агентов
    argos-claude find <task>                # Найти лучшего агента для задачи
    argos-claude agent <name>               # Показать детали агента
    argos-claude prompt <name>              # Получить системный промпт
    argos-claude list [category]             # Список агентов
    argos-claude categories                  # Список категорий
    argos-claude stats                      # Статистика
    argos-claude report                     # Полный отчёт
    argos-claude exec <command> [args]      # Выполнить команду
"""

import sys
import argparse
import json
from pathlib import Path

# Фикс кодировки вывода: консоль Windows в cp1251 не кодирует эмодзи/юникод
# (иначе UnicodeEncodeError 'charmap' на 🔍/🤖 и вывод падает).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Добавляем src в путь
sys.path.insert(0, str(Path(__file__).parent))

from src.argos_claude_api import ArgosClaudeAPI, AgentMatch


def format_agent(agent: dict or AgentMatch, detailed: bool = False) -> str:
    """Форматирование вывода агента."""
    if isinstance(agent, AgentMatch):
        lines = [
            f"🤖 {agent.name}",
            f"   📁 {agent.category}",
            f"   🎯 Confidence: {agent.confidence:.0%}",
            f"   📝 {agent.description[:80]}..." if len(agent.description) > 80 else f"   📝 {agent.description}",
        ]
    else:
        lines = [
            f"🤖 {agent['name']}",
            f"   📁 {agent['category']}",
        ]
        if 'tools' in agent and agent['tools']:
            lines.append(f"   🔧 {', '.join(agent['tools'][:5])}")
        lines.append(f"   📝 {agent['description'][:80]}..." if len(agent['description']) > 80 else f"   📝 {agent['description']}")
    
    if detailed:
        lines.append("")
        lines.append("   " + "-" * 50)
    
    return "\n".join(lines)


def cmd_search(args):
    """Поиск агентов по запросу."""
    print(f"🔍 Поиск агентов: '{args.query}'")
    print()
    
    api = ArgosClaudeAPI()
    results = api.search_agents(args.query)
    
    if not results:
        print(f"❌ Агенты не найдены")
        return 1
    
    print(f"Найдено: {len(results)}\n")
    for agent in results:
        print(format_agent(agent))
        print()
    
    return 0


def cmd_find(args):
    """Найти лучшего агента для задачи."""
    print(f"🎯 Поиск лучшего агента для: '{args.task}'")
    print()
    
    api = ArgosClaudeAPI()
    agent = api.find_agent(args.task)
    
    if not agent:
        print(f"❌ Подходящий агент не найден")
        return 1
    
    print(format_agent(agent, detailed=True))
    print()
    print("=" * 60)
    print("System Prompt:")
    print("=" * 60)
    print(agent.prompt[:1000] + "..." if len(agent.prompt) > 1000 else agent.prompt)
    
    return 0


def cmd_agent(args):
    """Показать детали агента."""
    print(f"🤖 Детали агента: '{args.name}'")
    print()
    
    api = ArgosClaudeAPI()
    prompt = api.get_agent_prompt(args.name)
    
    if not prompt:
        # Try search
        results = api.search_agents(args.name)
        if results:
            agent = results[0]
            prompt = api.get_agent_prompt(agent['name'])
            print(format_agent(agent, detailed=True))
            print()
        else:
            print(f"❌ Агент '{args.name}' не найден")
            return 1
    else:
        # List agents to get info
        agents = api.list_agents()
        agent_info = next((a for a in agents if a['name'] == args.name), None)
        if agent_info:
            print(format_agent(agent_info, detailed=True))
            print()
    
    if args.prompt:
        print("=" * 60)
        print("System Prompt:")
        print("=" * 60)
        print(prompt[:2000] + "..." if len(prompt) > 2000 else prompt)
    
    return 0


def cmd_prompt(args):
    """Получить системный промпт агента."""
    api = ArgosClaudeAPI()
    prompt = api.get_agent_prompt(args.name)
    
    if not prompt:
        # Try search and use first result
        results = api.search_agents(args.name)
        if results:
            prompt = api.get_agent_prompt(results[0]['name'])
    
    if not prompt:
        print(f"❌ Промпт для '{args.name}' не найден")
        return 1
    
    print(prompt)
    return 0


def cmd_list(args):
    """Список агентов."""
    print(f"📋 Список агентов")
    if args.category:
        print(f"   Фильтр: {args.category}")
    print()
    
    api = ArgosClaudeAPI()
    agents = api.list_agents(category=args.category, limit=args.limit)
    
    if not agents:
        print("❌ Агенты не найдены")
        return 1
    
    print(f"Показано: {len(agents)}\n")
    
    # Group by category
    by_cat = {}
    for agent in agents:
        by_cat.setdefault(agent['category'], []).append(agent)
    
    for category, cat_agents in sorted(by_cat.items()):
        print(f"📁 {category} ({len(cat_agents)})")
        for agent in cat_agents:
            print(f"   • {agent['name']}")
        print()
    
    return 0


def cmd_categories(args):
    """Список категорий."""
    print("📁 Категории агентов")
    print()
    
    api = ArgosClaudeAPI()
    agents = api.list_agents(limit=1000)
    
    from collections import Counter
    cats = Counter(a['category'] for a in agents)
    
    for cat, count in cats.most_common(20):
        print(f"  {cat:40s}: {count}")
    
    print(f"\nВсего категорий: {len(cats)}")
    
    return 0


def cmd_stats(args):
    """Статистика."""
    api = ArgosClaudeAPI()
    stats = api.get_stats()
    
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    
    return 0


def cmd_report(args):
    """Полный отчёт."""
    api = ArgosClaudeAPI()
    print(api.generate_report())
    return 0


def cmd_exec(args):
    """Выполнить команду."""
    print(f"⌨️ Команда: {args.command}")
    print(f"   Аргументы: {args.args or 'None'}")
    print()
    
    api = ArgosClaudeAPI()
    result = api.execute_command(args.command, *args.args or [])
    
    if result.success:
        print("✅ Успешно")
        print()
        print(result.output)
    else:
        print("❌ Ошибка")
        print(result.error)
        return 1
    
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="argos-claude",
        description="CLI для работы с Claude Code Templates в ARGOS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  %(prog)s search python
  %(prog)s find "создай FastAPI сервис"
  %(prog)s agent python-pro --prompt
  %(prog)s categories
  %(prog)s stats
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Команды", metavar="COMMAND")
    
    # search
    search_p = subparsers.add_parser("search", help="Поиск агентов")
    search_p.add_argument("query", help="Поисковый запрос")
    
    # find
    find_p = subparsers.add_parser("find", help="Найти агента для задачи")
    find_p.add_argument("task", help="Описание задачи")
    
    # agent
    agent_p = subparsers.add_parser("agent", help="Детали агента")
    agent_p.add_argument("name", help="Имя агента")
    agent_p.add_argument("--prompt", "-p", action="store_true", help="Показать промпт")
    
    # prompt
    prompt_p = subparsers.add_parser("prompt", help="Получить промпт агента")
    prompt_p.add_argument("name", help="Имя агента")
    
    # list
    list_p = subparsers.add_parser("list", help="Список агентов")
    list_p.add_argument("category", nargs="?", help="Фильтр по категории")
    list_p.add_argument("--limit", "-l", type=int, default=50, help="Лимит (по умолчанию 50)")
    
    # categories
    subparsers.add_parser("categories", help="Список категорий")
    
    # stats
    subparsers.add_parser("stats", help="Статистика")
    
    # report
    subparsers.add_parser("report", help="Полный отчёт")
    
    # exec
    exec_p = subparsers.add_parser("exec", help="Выполнить команду")
    exec_p.add_argument("command", help="Имя команды")
    exec_p.add_argument("args", nargs="*", help="Аргументы команды")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    commands = {
        "search": cmd_search,
        "find": cmd_find,
        "agent": cmd_agent,
        "prompt": cmd_prompt,
        "list": cmd_list,
        "categories": cmd_categories,
        "stats": cmd_stats,
        "report": cmd_report,
        "exec": cmd_exec,
    }
    
    try:
        return commands[args.command](args)
    except KeyboardInterrupt:
        print("\n\nОтменено пользователем")
        return 130
    except Exception as e:
        print(f"\n❌ Ошибка: {e}", file=sys.stderr)
        if hasattr(args, 'verbose') and args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())