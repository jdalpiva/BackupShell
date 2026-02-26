#!/usr/bin/env python3
"""
Backup Automatizado - Versão Python
====================================
Programa para realizar backup incremental da pasta pessoal
para um HD externo, com notificações, rotação de backups,
verificação de espaço em disco e logging profissional.

Uso:
    python3 backup_automatizado.py
    python3 backup_automatizado.py --dry-run
    python3 backup_automatizado.py --config /caminho/para/config.yaml
    python3 backup_automatizado.py --verbose
"""

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("[ERRO] Módulo 'pyyaml' não encontrado. Instale com: pip install pyyaml")
    sys.exit(1)


# ─── Configuração ────────────────────────────────────────────────────────────

CONFIG_PADRAO = Path(__file__).parent / "backup_config.yaml"

EXCLUSOES_PADRAO = [
    ".cache/*",
    "Trash/*",
    ".local/share/Trash/*",
    "__pycache__/*",
    "*.tmp",
    ".venv/*",
    "node_modules/*",
    ".thumbnails/*",
    "snap/*",
]


@dataclass
class BackupConfig:
    """Configurações carregadas do arquivo YAML."""

    origem: str = ""
    destino: str = ""
    log_dir: str = "~/logs/backup"
    retencao_dias: int = 30
    espaco_minimo_gb: float = 5.0
    exclusoes: list[str] = field(default_factory=lambda: list(EXCLUSOES_PADRAO))

    @classmethod
    def carregar(cls, caminho: Path) -> "BackupConfig":
        """Carrega configuração a partir de um arquivo YAML."""
        caminho = caminho.expanduser().resolve()
        if not caminho.exists():
            raise FileNotFoundError(f"Arquivo de configuração não encontrado: {caminho}")

        with open(caminho, "r", encoding="utf-8") as f:
            dados = yaml.safe_load(f) or {}

        return cls(
            origem=dados.get("origem", cls.origem),
            destino=dados.get("destino", cls.destino),
            log_dir=dados.get("log_dir", cls.log_dir),
            retencao_dias=dados.get("retencao_dias", cls.retencao_dias),
            espaco_minimo_gb=dados.get("espaco_minimo_gb", cls.espaco_minimo_gb),
            exclusoes=dados.get("exclusoes", list(EXCLUSOES_PADRAO)),
        )


# ─── Notificações ────────────────────────────────────────────────────────────


def enviar_notificacao(titulo: str, mensagem: str, urgencia: str = "normal") -> None:
    """
    Envia notificação desktop via notify-send.

    Args:
        titulo: Título da notificação.
        mensagem: Corpo da notificação.
        urgencia: Nível de urgência ('low', 'normal', 'critical').
    """
    try:
        subprocess.run(
            ["notify-send", f"--urgency={urgencia}", titulo, mensagem],
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        # notify-send não disponível (ex.: sistema sem desktop)
        pass
    except subprocess.TimeoutExpired:
        pass


# ─── Logger ───────────────────────────────────────────────────────────────────


def configurar_logger(log_dir: str, verbose: bool = False) -> logging.Logger:
    """
    Configura logger com saída para arquivo (rotativo) e terminal.

    Args:
        log_dir: Diretório onde salvar os logs.
        verbose: Se True, mostra DEBUG no terminal.

    Returns:
        Logger configurado.
    """
    log_path = Path(log_dir).expanduser().resolve()
    log_path.mkdir(parents=True, exist_ok=True)
    arquivo_log = log_path / "backup.log"

    logger = logging.getLogger("backup_automatizado")
    logger.setLevel(logging.DEBUG)

    # Handler para arquivo (rotação: 5 MB, mantém 3 arquivos)
    fh = RotatingFileHandler(
        arquivo_log, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fmt_arquivo = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(fmt_arquivo)
    logger.addHandler(fh)

    # Handler para terminal
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt_terminal = logging.Formatter("[%(levelname)s] %(message)s")
    ch.setFormatter(fmt_terminal)
    logger.addHandler(ch)

    return logger


# ─── Gerenciador de Backup ────────────────────────────────────────────────────


class BackupManager:
    """Orquestra todo o processo de backup."""

    def __init__(self, config: BackupConfig, logger: logging.Logger, dry_run: bool = False):
        self.config = config
        self.logger = logger
        self.dry_run = dry_run
        self._interrompido = False
        self._backup_dir: Optional[Path] = None

        # Captura sinais para limpeza adequada
        signal.signal(signal.SIGINT, self._handler_sinal)
        signal.signal(signal.SIGTERM, self._handler_sinal)

    def _handler_sinal(self, signum: int, frame) -> None:
        """Trata interrupções (Ctrl+C, SIGTERM) de forma limpa."""
        nome_sinal = signal.Signals(signum).name
        self.logger.warning(f"Sinal {nome_sinal} recebido. Interrompendo backup...")
        self._interrompido = True
        enviar_notificacao(
            "⚠️ Backup Interrompido",
            f"O backup foi interrompido pelo sinal {nome_sinal}.",
            urgencia="critical",
        )
        sys.exit(130)

    # ── Verificações ──────────────────────────────────────────────────────

    def verificar_hd_montado(self) -> bool:
        """Verifica se o HD de destino está montado."""
        destino = Path(self.config.destino)

        if not destino.exists():
            self.logger.error(f"Diretório de destino não existe: {destino}")
            enviar_notificacao(
                "❌ Backup Falhou",
                f"O diretório de destino não existe:\n{destino}",
                urgencia="critical",
            )
            return False

        resultado = subprocess.run(
            ["mountpoint", "-q", str(destino)],
            capture_output=True,
        )

        if resultado.returncode != 0:
            self.logger.error(f"HD externo não está montado em: {destino}")
            enviar_notificacao(
                "❌ Backup Falhou",
                f"O HD externo não está montado em:\n{destino}\n\nConecte o HD e tente novamente.",
                urgencia="critical",
            )
            return False

        self.logger.info(f"HD externo montado em: {destino}")
        return True

    def verificar_espaco_disco(self) -> bool:
        """
        Verifica se há espaço suficiente no HD de destino.

        Compara o espaço livre com o mínimo configurado em espaco_minimo_gb.
        Se o espaço for insuficiente, envia notificação e retorna False.
        """
        destino = Path(self.config.destino)

        try:
            uso = shutil.disk_usage(destino)
        except OSError as e:
            self.logger.error(f"Erro ao verificar espaço em disco: {e}")
            enviar_notificacao(
                "❌ Backup Falhou",
                f"Não foi possível verificar o espaço em disco:\n{e}",
                urgencia="critical",
            )
            return False

        total_gb = uso.total / (1024 ** 3)
        usado_gb = uso.used / (1024 ** 3)
        livre_gb = uso.free / (1024 ** 3)
        percentual_usado = (uso.used / uso.total) * 100

        self.logger.info(
            f"Espaço em disco — Total: {total_gb:.1f} GB | "
            f"Usado: {usado_gb:.1f} GB ({percentual_usado:.1f}%) | "
            f"Livre: {livre_gb:.1f} GB"
        )

        if livre_gb < self.config.espaco_minimo_gb:
            msg = (
                f"Espaço insuficiente no HD de destino!\n\n"
                f"📊 Livre: {livre_gb:.1f} GB\n"
                f"📊 Mínimo necessário: {self.config.espaco_minimo_gb:.1f} GB\n"
                f"📊 Usado: {percentual_usado:.1f}%\n\n"
                f"O backup NÃO será realizado.\n"
                f"Libere espaço no HD ou ajuste o parâmetro\n"
                f"'espaco_minimo_gb' no arquivo de configuração."
            )
            self.logger.error(
                f"HD CHEIO — Espaço livre: {livre_gb:.1f} GB, "
                f"mínimo necessário: {self.config.espaco_minimo_gb:.1f} GB. "
                f"Backup cancelado."
            )
            enviar_notificacao(
                "🚫 HD Cheio — Backup Cancelado",
                msg,
                urgencia="critical",
            )
            return False

        # Aviso quando está perto do limite (menos de 2x o mínimo)
        if livre_gb < self.config.espaco_minimo_gb * 2:
            self.logger.warning(
                f"Espaço em disco baixo! Livre: {livre_gb:.1f} GB. "
                f"Considere liberar espaço em breve."
            )
            enviar_notificacao(
                "⚠️ Espaço em Disco Baixo",
                f"O HD está com apenas {livre_gb:.1f} GB livres.\n"
                f"Considere liberar espaço em breve.",
                urgencia="normal",
            )

        return True

    # ── Backup ────────────────────────────────────────────────────────────

    def executar_rsync(self) -> bool:
        """
        Executa o rsync para realizar o backup incremental.

        Returns:
            True se o rsync executou com sucesso, False caso contrário.
        """
        data = datetime.now().strftime("%Y-%m-%d")
        self._backup_dir = Path(self.config.destino) / f"backup_{data}"

        if not self.dry_run:
            self._backup_dir.mkdir(parents=True, exist_ok=True)

        # Monta o comando rsync
        cmd = [
            "rsync",
            "-rltvh",
            "--progress",
            "--delete",
            "--no-perms",
            "--no-owner",
            "--no-group",
        ]

        if self.dry_run:
            cmd.append("--dry-run")
            self.logger.info("🔍 Modo DRY-RUN ativado — nenhum arquivo será copiado.")

        # Adiciona exclusões
        for exclusao in self.config.exclusoes:
            cmd.append(f"--exclude={exclusao}")

        cmd.append(self.config.origem)
        cmd.append(str(self._backup_dir))

        self.logger.info(f"Iniciando rsync: {self.config.origem} → {self._backup_dir}")
        self.logger.debug(f"Comando: {' '.join(cmd)}")

        inicio = datetime.now()

        try:
            resultado = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            self.logger.error(f"Erro ao executar rsync: {e}")
            enviar_notificacao(
                "❌ Backup Falhou",
                f"Erro ao executar rsync:\n{e}",
                urgencia="critical",
            )
            return False

        duracao = datetime.now() - inicio

        # Registra a saída do rsync no log
        if resultado.stdout:
            for linha in resultado.stdout.strip().split("\n")[-20:]:
                self.logger.debug(f"rsync: {linha}")

        if resultado.returncode == 0:
            self.logger.info(
                f"✅ Backup concluído com sucesso! Duração: {self._formatar_duracao(duracao)}"
            )
            self._registrar_resumo(duracao)
            return True
        else:
            erro = resultado.stderr.strip() if resultado.stderr else "Erro desconhecido"
            self.logger.error(f"rsync falhou (código {resultado.returncode}): {erro}")
            enviar_notificacao(
                "❌ Backup Falhou",
                f"O rsync retornou erro (código {resultado.returncode}).\n"
                f"Verifique o log para detalhes.",
                urgencia="critical",
            )
            return False

    def _registrar_resumo(self, duracao: timedelta) -> None:
        """Registra um resumo do backup no log."""
        if self._backup_dir and self._backup_dir.exists() and not self.dry_run:
            try:
                # Conta arquivos e calcula tamanho
                total_arquivos = 0
                tamanho_total = 0
                for item in self._backup_dir.rglob("*"):
                    if item.is_file():
                        total_arquivos += 1
                        tamanho_total += item.stat().st_size

                tamanho_gb = tamanho_total / (1024 ** 3)
                self.logger.info(
                    f"📋 Resumo: {total_arquivos:,} arquivos | "
                    f"{tamanho_gb:.2f} GB | "
                    f"Duração: {self._formatar_duracao(duracao)}"
                )
            except OSError as e:
                self.logger.warning(f"Não foi possível calcular resumo: {e}")

    # ── Rotação ───────────────────────────────────────────────────────────

    def rotacionar_backups(self) -> None:
        """Remove backups mais antigos que o período de retenção configurado."""
        if self.dry_run:
            self.logger.info("🔍 Dry-run: pulando rotação de backups.")
            return

        destino = Path(self.config.destino)
        limite = datetime.now() - timedelta(days=self.config.retencao_dias)
        removidos = 0

        self.logger.info(
            f"Verificando backups mais antigos que {self.config.retencao_dias} dias "
            f"(antes de {limite.strftime('%Y-%m-%d')})..."
        )

        for pasta in sorted(destino.iterdir()):
            if not pasta.is_dir() or not pasta.name.startswith("backup_"):
                continue

            try:
                # Extrai a data do nome da pasta (backup_YYYY-MM-DD)
                data_str = pasta.name.replace("backup_", "")
                data_backup = datetime.strptime(data_str, "%Y-%m-%d")

                if data_backup < limite:
                    self.logger.info(f"Removendo backup antigo: {pasta.name}")
                    shutil.rmtree(pasta)
                    removidos += 1
            except ValueError:
                # Nome de pasta não segue o padrão esperado, ignora
                continue
            except OSError as e:
                self.logger.error(f"Erro ao remover {pasta.name}: {e}")

        if removidos > 0:
            self.logger.info(f"🗑️  {removidos} backup(s) antigo(s) removido(s).")
        else:
            self.logger.info("Nenhum backup antigo para remover.")

    # ── Utilitários ───────────────────────────────────────────────────────

    @staticmethod
    def _formatar_duracao(duracao: timedelta) -> str:
        """Formata timedelta para leitura humana."""
        total_seg = int(duracao.total_seconds())
        horas, resto = divmod(total_seg, 3600)
        minutos, segundos = divmod(resto, 60)

        if horas > 0:
            return f"{horas}h {minutos}min {segundos}s"
        elif minutos > 0:
            return f"{minutos}min {segundos}s"
        else:
            return f"{segundos}s"

    # ── Execução Principal ────────────────────────────────────────────────

    def executar(self) -> bool:
        """
        Orquestra todo o processo de backup.

        Returns:
            True se o backup foi concluído com sucesso, False caso contrário.
        """
        self.logger.info("=" * 60)
        self.logger.info(f"BACKUP INICIADO em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if self.dry_run:
            self.logger.info("*** MODO DRY-RUN — NENHUM ARQUIVO SERÁ MODIFICADO ***")
        self.logger.info("=" * 60)

        # 1. Verificar se o HD está montado
        self.logger.info("Etapa 1/4: Verificando se o HD está montado...")
        if not self.verificar_hd_montado():
            return False

        # 2. Verificar espaço em disco
        self.logger.info("Etapa 2/4: Verificando espaço em disco...")
        if not self.verificar_espaco_disco():
            return False

        # 3. Executar backup
        self.logger.info("Etapa 3/4: Executando backup com rsync...")
        sucesso = self.executar_rsync()

        # 4. Rotacionar backups antigos
        if sucesso:
            self.logger.info("Etapa 4/4: Verificando backups antigos para rotação...")
            self.rotacionar_backups()

            if not self.dry_run:
                enviar_notificacao(
                    "✅ Backup Concluído",
                    f"Backup de {self.config.origem} realizado com sucesso em "
                    f"{self._backup_dir}.",
                    urgencia="low",
                )

        self.logger.info("=" * 60)
        self.logger.info(
            f"BACKUP FINALIZADO — Status: {'SUCESSO ✅' if sucesso else 'FALHA ❌'}"
        )
        self.logger.info("=" * 60)

        return sucesso


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Ponto de entrada principal via linha de comando."""
    parser = argparse.ArgumentParser(
        description="Backup Automatizado — Versão Python",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  python3 backup_automatizado.py                    # Backup normal\n"
            "  python3 backup_automatizado.py --dry-run          # Simular sem copiar\n"
            "  python3 backup_automatizado.py --verbose          # Saída detalhada\n"
            "  python3 backup_automatizado.py --config outro.yaml # Config customizada\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula o backup sem copiar arquivos.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PADRAO,
        help=f"Caminho para o arquivo de configuração YAML (padrão: {CONFIG_PADRAO}).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Mostra mensagens de DEBUG no terminal.",
    )

    args = parser.parse_args()

    # Carrega configuração
    try:
        config = BackupConfig.carregar(args.config)
    except FileNotFoundError as e:
        print(f"[ERRO] {e}")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"[ERRO] Erro ao ler arquivo YAML: {e}")
        sys.exit(1)

    # Valida campos obrigatórios
    if not config.origem or not config.destino:
        print("[ERRO] 'origem' e 'destino' devem ser definidos no arquivo de configuração.")
        sys.exit(1)

    # Configura logger
    logger = configurar_logger(config.log_dir, verbose=args.verbose)

    # Executa backup
    manager = BackupManager(config, logger, dry_run=args.dry_run)
    sucesso = manager.executar()

    sys.exit(0 if sucesso else 1)


if __name__ == "__main__":
    main()
