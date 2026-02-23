import yaml
import logging
from auto_reger.proxy_api import ProxyApi

# Включаем логирование, чтобы видеть магию под капотом
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def main():
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        print("[-] ОШИБКА: Файл config.yaml не найден!")
        return

    country = config.get("registration", {}).get("default_country", "US")
    print(f"\n[~] Тестируем резидентные прокси для страны: {country}")
    
    proxy_api = ProxyApi()
    
    print("\n[~] Запрашиваем 1-й прокси (может занять время, если список создается впервые)...")
    proxy1 = proxy_api.get_proxy(country_code=country)
    
    if proxy1:
        print(f"\n[+] УСПЕХ! Получен 1-й прокси: {proxy1}")
        
        print("\n[~] Запрашиваем 2-й прокси (должен мгновенно взяться из кэша)...")
        proxy2 = proxy_api.get_proxy(country_code=country)
        print(f"[+] УСПЕХ! Получен 2-й прокси: {proxy2}")
        
        print(f"\n[+] Ссылка-интент для первого прокси будет: tg://socks?server={proxy1['ip']}&port={proxy1['port']}")
    else:
        print("\n[-] ПРОВАЛ: Прокси не получен. Смотри логи выше для деталей.")

if __name__ == "__main__":
    main()