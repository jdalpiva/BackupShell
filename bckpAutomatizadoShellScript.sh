#!/bin/bash

# Programa para realizar backup semanalmente da minha pasta pessoal

origem="/home/jdalpiva/" # Pasta que vai ser realizada o backup
destino="/mnt/Dados" # Pasta de destino do backup
data=$(date +%Y-%m-%d) # Formato da data e horario
log="$HOME/backup_pessoal.log" # Arquivo de log

# Faz a verificação se o HD está montado
if ! mountpoint -q "$destino"; then 
	echo "[ERRO] $(date): HD externo não montado na pasta $destino" >> "$log"
	exit 1
fi

# Cria paasta de backup com data
backup_dir="$destino/backup_$data"
mkdir -p "$backup_dir"

# Registra inicio no log 
echo "=== Backup iniciado em: $(date) ===" >> "$log"

# Executa o rsync (backup incremental) - Só realiza backup que foram alterados
rsync -rltvh --progress --delete \
	--no-perms --no-owner --no-group \
	--exclude='.cache/*' \
	--exclude='Trash/*'  \
	"$origem" "$backup_dir" >> "$log" 2>&1

# Verifica se o rsync foi bem sucedido 
if [ $? -eq 0 ]; then
	echo "Backup concluido com sucesso em: $(date)" >> "$log"
	ls -R "$backup_dir" >> "$log" #Lista os arquivos copiads
else 
	echo "[ERRO] $(date): Falha durante o rsync. Verifique o log." >> "$log"
fi

echo "==========================================" >> "$log"
