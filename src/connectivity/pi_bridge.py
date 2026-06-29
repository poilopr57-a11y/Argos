"""
pi_bridge.py — Мост между ARGOS и Pi Coding Agent

Позволяет ARGOS:
  • Запускать Pi с задачами через CLI
  • Получать результаты выполнения
  • Управлять сессиями Pi
  • Интегрировать Pi как внешний агент-кодер

Требования: npm install -g @mariozechner/pi-coding-agent
"""

import os
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

from src.argos_logger import get_logger

log = get_logger("argos.pi_bridge")


@dataclass
class PiTask:
    """Задача для Pi."""
    id: str
    prompt: str
    cwd: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    files: List[str] = field(default_factory=list)
    status: str = "pending"  # pending, running, completed, failed
    result: str = ""
    error: str = ""


class PiBridge:
    """Мост для управления Pi Coding Agent из ARGOS."""

    @staticmethod
    def _env_enabled(name: str, default: str = "1") -> bool:
        value = os.getenv(name, default).strip().lower()
        return value not in ("0", "false", "off", "no", "нет", "выкл")

    def _build_gpu_instances(self) -> list[dict]:
        instances: list[dict] = []
        if self._env_enabled("OLLAMA_ENABLED", "false"):
            instances.append(
                {
                    "name": "Unified Ollama",
                    "url": os.getenv("OLLAMA_HOST", "http://localhost:11434"),
                    "api": "ollama",
                    "models": [
                        os.getenv("OLLAMA_FAST_MODEL", "qwen2.5:3b"),
                        os.getenv("OLLAMA_MODEL", "argos-v1"),
                    ],
                }
            )
        for idx in range(50):
            if not self._env_enabled(f"GPU_SERVER_{idx}_ENABLED", "1"):
                continue
            host = os.getenv(f"GPU_SERVER_{idx}_HOST", "localhost").strip()
            port = os.getenv(f"GPU_SERVER_{idx}_PORT", "").strip()
            if not host or not port:
                continue
            model = os.getenv(f"GPU_SERVER_{idx}_MODEL", "unknown").strip()
            name = os.getenv(f"GPU_SERVER_{idx}_NAME", f"GPU{idx}").strip()
            instances.append(
                {
                    "name": name,
                    "url": f"http://{host}:{port}",
                    "api": "llamacpp",
                    "models": [model] if model else [],
                }
            )
        return instances

    @staticmethod
    def _find_pi_path() -> str:
        """Найти путь к Pi."""
        import shutil
        # Сначала в PATH
        if shutil.which("pi"):
            return "pi"
        # Windows npm путь
        npm_path = r"C:\Users\AvA\AppData\Roaming\npm\pi.cmd"
        if os.path.exists(npm_path):
            return npm_path
        # Другие пути
        for path in [r"C:\Users\AvA\AppData\Roaming\npm\pi",
                     r"C:\Program Files\npm\pi.cmd",
                     r"C:\Program Files (x86)\npm\pi.cmd"]:
            if os.path.exists(path):
                return path
        return "pi"  # fallback

    def __init__(self, 
                 pi_path: str = None,
                 default_model: str = None,  # None = использовать default Pi
                 default_cwd: str = None):
        self.pi_path = pi_path or self._find_pi_path()
        self.default_model = default_model
        self.default_cwd = default_cwd or os.getcwd()
        self._tasks: Dict[str, PiTask] = {}
        self._lock = threading.Lock()
        self._api_key = os.getenv("KIMI_API_KEY", "")  # Резервный ключ
        # Локальные GPU + VM кластер
        self._gpu_instances = self._build_gpu_instances() + [
            {"name": "JP1 VM", "url": os.getenv("OLLAMA_JP1_HOST", "http://10.200.0.2:11434"), "api": "ollama", "models": []},
            {"name": "JP2 VM", "url": os.getenv("OLLAMA_JP2_HOST", "http://10.200.0.3:11434"), "api": "ollama", "models": []},
            {"name": "Azure VM", "url": os.getenv("OLLAMA_AZURE_HOST", "http://10.200.0.4:11434"), "api": "ollama", "models": []},
            {"name": "Sweden VM", "url": "http://localhost:11440", "api": "ollama", "models": []},
        ]
        # Endpoint discovery can take tens of seconds when remote VPN nodes are
        # offline. Keep status/diagnostics cheap and probe only for inference.
        self._active_instance = {
            "name": "Unprobed",
            "url": os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            "api": "ollama",
            "models": [],
        }
        self._check_installation()

    def _probe_instance(self, inst: dict) -> Optional[dict]:
        """Проверить Ollama или llama.cpp/OpenAI-compatible endpoint."""
        import requests as _req

        url = str(inst.get("url", "")).rstrip("/")
        if not url:
            return None

        try:
            resp = _req.get(f"{url}/api/tags", timeout=3)
            if resp.ok:
                models = [m.get("name", "") for m in resp.json().get("models", []) if m.get("name")]
                checked = dict(inst)
                checked["api"] = "ollama"
                checked["models"] = models or inst.get("models", [])
                return checked
        except Exception:
            pass

        try:
            resp = _req.get(f"{url}/v1/models", timeout=3)
            if resp.ok:
                data = resp.json().get("data", [])
                models = [m.get("id", "") for m in data if m.get("id")]
                checked = dict(inst)
                checked["api"] = "llamacpp"
                checked["models"] = models or inst.get("models", [])
                return checked
        except Exception:
            pass

        try:
            resp = _req.get(f"{url}/health", timeout=3)
            if resp.ok:
                checked = dict(inst)
                checked["api"] = "llamacpp"
                checked["models"] = inst.get("models", [])
                return checked
        except Exception:
            pass
        return None

    def _find_best_instance(self) -> dict:
        """Найти лучший доступный инстанс."""
        for inst in self._gpu_instances:
            checked = self._probe_instance(inst)
            if checked:
                return checked
        return {"name": "Default", "url": "http://localhost:11434", "api": "ollama", "models": []}

    def list_local_models(self) -> list:
        """Получить список всех доступных моделей."""
        models = []
        for inst in self._gpu_instances:
            checked = self._probe_instance(inst)
            if checked:
                for m in checked.get("models", []) or ["local-model"]:
                    models.append(f"{checked['name']}/{m}")
        return models

    def list_instances(self) -> str:
        """Статус всех инстансов."""
        lines = ["=== Ollama Instances ==="]
        available = 0
        for inst in self._gpu_instances:
            checked = self._probe_instance(inst)
            if checked:
                model_count = len(checked.get("models", []))
                status = f"OK {checked.get('api', 'api')} ({model_count} models)"
                available += 1
            else:
                status = "Offline"
            lines.append(f"  {inst['name']}: {status}")
        lines.append(f"\nAvailable: {available}/{len(self._gpu_instances)}")
        return "\n".join(lines)

    def _ask_ollama(self, prompt: str, model: str = "qwen2.5:7b", instance_url: str = None, timeout: int = 120) -> str:
        """Локальный запрос к Ollama/llama.cpp."""
        import requests as _req
        if instance_url:
            inst = {"name": "custom", "url": instance_url, "models": [model]}
        else:
            if self._active_instance.get("name") == "Unprobed":
                self._active_instance = self._find_best_instance()
            inst = self._active_instance
        checked = self._probe_instance(inst) or inst
        url = checked.get("url", "http://localhost:11434").rstrip("/")
        api = checked.get("api", "ollama")
        selected_model = model or (checked.get("models") or ["local-model"])[0]
        try:
            if api == "llamacpp":
                resp = _req.post(
                    f"{url}/v1/chat/completions",
                    json={
                        "model": selected_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                        "max_tokens": 1200,
                        "stream": False,
                    },
                    timeout=timeout,
                )
                if resp.ok:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        return (msg.get("content") or choices[0].get("text") or "").strip()
                return f"[llama.cpp] Error: {resp.status_code} {resp.text[:200]}"

            resp = _req.post(
                f"{url}/api/chat",
                json={"model": selected_model, "messages": [{"role": "user", "content": prompt}], "stream": False},
                timeout=timeout
            )
            if resp.ok:
                data = resp.json()
                return data.get("message", {}).get("content", "") or data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
            return f"[Ollama] Error: {resp.status_code}"
        except Exception as e:
            return f"[Ollama] Error: {e}"

    def execute_local(self, prompt: str, model: str = "qwen2.5:7b", timeout: int = 120) -> str:
        """Выполнить через локальную Ollama."""
        log.info(f"[PiBridge] Local request: model={model}, instance={self._active_instance['name']}")
        return self._ask_ollama(prompt, model, timeout=timeout)

    @staticmethod
    def _run_cmd(args: list, timeout: int = 30, cwd: str = None) -> subprocess.CompletedProcess:
        """Запуск команды с учётом Windows .cmd файлов."""
        import platform
        import shutil
        is_windows = platform.system() == "Windows"
        
        # Проверяем: нужно ли использовать shell на Windows
        # shell=True нужен для: .cmd/.bat/.ps1 файлов И для команд без расширения найденных через PATH
        cmd_path = args[0]
        needs_shell = False
        
        if is_windows:
            # Если это .cmd/.bat/.ps1 файл
            if any(cmd_path.lower().endswith(ext) for ext in (".cmd", ".bat", ".ps1")):
                needs_shell = True
            # Или если команда без расширения - проверим PATH
            elif not any(cmd_path.lower().endswith(ext) for ext in (".exe", ".com", ".py", ".jar")):
                # Ищем реальный путь к команде
                real_path = shutil.which(cmd_path)
                if real_path and any(real_path.lower().endswith(ext) for ext in (".cmd", ".bat", ".ps1")):
                    args = [real_path] + args[1:]
                    needs_shell = True
                elif not real_path:
                    # Команда не найдена в PATH - попробуем через shell (он найдёт в PATH)
                    needs_shell = True
        
        if needs_shell:
            cmd_str = " ".join(args)
            return subprocess.run(
                cmd_str, 
                shell=True, 
                capture_output=True, 
                text=True, 
                timeout=timeout,
                cwd=cwd
            )
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)

    def _check_installation(self):
        """Проверить установку Pi."""
        try:
            result = self._run_cmd([self.pi_path, "--version"], timeout=10)
            if result.returncode == 0:
                version = (result.stdout.strip() or result.stderr.strip())
                log.info(f"Pi detected: {version}")
            else:
                log.warning("Pi installed but returned error")
        except FileNotFoundError:
            log.warning(f"Pi not found at '{self.pi_path}'. Install: npm install -g @mariozechner/pi-coding-agent")
        except Exception as e:
            log.warning(f"Pi check error: {e}")

    @property
    def is_available(self) -> bool:
        """Проверить доступность Pi."""
        try:
            path = self.pi_path or self._find_pi_path()
            result = self._run_cmd([path, "--version"], timeout=5)
            return result.returncode == 0
        except:
            return False

    def execute(self, 
                prompt: str,
                cwd: Optional[str] = None,
                model: Optional[str] = None,
                system_prompt: Optional[str] = None,
                files: Optional[List[str]] = None,
                timeout: int = 300) -> str:
        """
        Выполнить задачу через Pi и вернуть результат.
        
        Args:
            prompt: Текстовый промпт для Pi
            cwd: Рабочая директория
            model: Модель ИИ (например, 'kimi-k2.5:cloud')
            system_prompt: Системный промпт
            files: Список файлов для контекста
            timeout: Таймаут в секундах
        
        Returns:
            Результат выполнения
        """
        if not self.is_available:
            return "[PiBridge] Ошибка: Pi не установлен. Установи: npm install -g @mariozechner/pi-coding-agent"

        task_id = f"pi_{int(time.time() * 1000)}"
        task = PiTask(
            id=task_id,
            prompt=prompt,
            cwd=cwd or self.default_cwd,
            model=model or self.default_model,
            system_prompt=system_prompt,
            files=files or []
        )

        with self._lock:
            self._tasks[task_id] = task

        try:
            task.status = "running"
            result = self._run_pi_task(task, timeout)
            task.status = "completed"
            task.result = result
            return result
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            log.error(f"Pi task {task_id} failed: {e}")
            return f"[PiBridge] Ошибка: {e}"

    def _run_pi_task(self, task: PiTask, timeout: int) -> str:
        """Запустить Pi с задачей."""
        # Создаём временный файл с промптом
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            if task.system_prompt:
                f.write(f"<!-- system: {task.system_prompt} -->\n\n")
            f.write(task.prompt)
            prompt_file = f.name

        try:
            # Формируем команду
            cmd = [
                self.pi_path,
                "--print",
                "--no-session"
            ]
            
            # Добавляем модель если указана
            if task.model:
                cmd.extend(["--model", task.model])
                # Добавляем API ключ только если используем Kimi
                if "moonshotai" in task.model or "kimi" in task.model.lower():
                    if self._api_key:
                        cmd.extend(["--api-key", self._api_key])

            # Добавляем файлы в контекст
            if task.files:
                for file_path in task.files:
                    cmd.extend(["--append-system-prompt", file_path])

            # Добавляем промпт в конец
            cmd.append(task.prompt)

            log.info(f"Running Pi: {self.pi_path} --print ...")

            # Запускаем Pi с Windows-совместимым вызовом
            result = self._run_cmd(cmd, timeout=timeout, cwd=task.cwd)

            output = result.stdout
            if result.stderr and not result.stdout:
                output = result.stderr

            if result.returncode != 0 and not output:
                return f"[PiBridge] Exit code {result.returncode}: {result.stderr[:500]}"

            return output or "[PiBridge] Пустой ответ"

        finally:
            # Удаляем временный файл
            try:
                os.unlink(prompt_file)
            except:
                pass

    def execute_async(self,
                     prompt: str,
                     callback: Optional[Callable[[str], None]] = None,
                     **kwargs) -> str:
        """
        Асинхронное выполнение задачи через Pi.
        
        Returns:
            ID задачи
        """
        task_id = f"pi_{int(time.time() * 1000)}"
        
        def run_task():
            result = self.execute(prompt, **kwargs)
            if callback:
                callback(result)

        thread = threading.Thread(target=run_task, name=f"PiTask-{task_id}")
        thread.start()
        return task_id

    def get_task_status(self, task_id: str) -> Optional[Dict]:
        """Получить статус задачи."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            return {
                "id": task.id,
                "status": task.status,
                "prompt": task.prompt[:100] + "..." if len(task.prompt) > 100 else task.prompt,
                "result": task.result[:500] if task.result else "",
                "error": task.error
            }

    def list_models(self) -> str:
        """Получить список доступных моделей Pi и Ollama."""
        lines = []
        
        # Pi модели
        if self.is_available:
            try:
                result = self._run_cmd([self.pi_path, "--list-models"], timeout=30)
                lines.append("=== Pi Models ===")
                lines.append(result.stdout or result.stderr or "[empty]")
            except Exception as e:
                lines.append(f"[Pi] Error: {e}")
        
        # Ollama модели
        local = self.list_local_models()
        lines.append("\n=== Local Ollama Models ===")
        if local:
            for m in local:
                lines.append(f"  - {m}")
        else:
            lines.append("  (none)")
        
        return "\n".join(lines)

    def execute_ollama(self, prompt: str, model: str = "qwen2.5:7b", timeout: int = 120) -> str:
        """Выполнить через локальную Ollama модель."""
        log.info(f"[PiBridge] Ollama request: model={model}")
        return self._ask_ollama(prompt, model, timeout=timeout)

    def status(self) -> str:
        """Статус моста Pi."""
        avail = "доступен" if self.is_available else "недоступен"
        tasks = len(self._tasks)
        running = sum(1 for t in self._tasks.values() if t.status == "running")
        
        lines = [
            f"Pi Bridge: {avail}",
            f"  Path: {self.pi_path}",
            f"  Default model: {self.default_model}",
            f"  Active local model endpoint: {self._active_instance.get('name')} ({self._active_instance.get('api')})",
            f"  Tasks: {tasks} (running: {running})",
        ]
        
        if self._tasks:
            lines.append("  Recent tasks:")
            for task in list(self._tasks.values())[-5:]:
                status_icon = "✓" if task.status == "completed" else "⟳" if task.status == "running" else "✗"
                lines.append(f"    {status_icon} {task.id}: {task.status}")
        
        return "\n".join(lines)


def handle(command: str) -> str:
    """SkillLoader dispatch hook."""
    parts = command.strip().split(maxsplit=2)
    action = parts[0] if parts else "status"
    
    bridge = PiBridge()
    
    if action in ("статус", "status"):
        return bridge.status()
    
    elif action in ("модели", "models"):
        return bridge.list_models()
    
    elif action in ("запусти", "run", "execute") and len(parts) >= 2:
        prompt = parts[1]
        return bridge.execute(prompt)
    
    elif action in ("задачи", "tasks"):
        if len(bridge._tasks) == 0:
            return "Нет активных задач"
        lines = ["Задачи Pi:"]
        for tid, task in bridge._tasks.items():
            lines.append(f"  {tid}: {task.status}")
        return "\n".join(lines)
    
    else:
        return (
            "Pi Bridge — команды:\n"
            "  status — статус моста\n"
            "  models — список моделей\n"
            "  run <prompt> — выполнить задачу\n"
            "  tasks — список задач"
        )


if __name__ == "__main__":
    import sys
    
    bridge = PiBridge()
    print(bridge.status())
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "run" and len(sys.argv) > 2:
            print(bridge.execute(sys.argv[2]))
        elif cmd == "models":
            print(bridge.list_models())
