#!/system/bin/sh
# Этот скрипт скачивает и ставит Zygisk-LSPosed

LSP_URL="https://github.com/LSPosed/LSPosed/releases/download/v1.9.2/LSPosed-v1.9.2-7024-zygisk-release.zip"
LSP_ZIP="/data/local/tmp/lsposed.zip"

echo "Скачиваем LSPosed..."
curl -L $LSP_URL -o $LSP_ZIP

echo "Устанавливаем модуль через Magisk..."
magisk --install-module $LSP_ZIP

echo "Установка завершена. Требуется перезагрузка Zygote."
rm $LSP_ZIP