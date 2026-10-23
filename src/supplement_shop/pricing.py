from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP


def decimal_value(value, *, minimum=Decimal('0'), maximum=Decimal('1000000000')) -> Decimal:
    try:
        number = Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise ValueError('Введите корректное число.') from None
    if not number.is_finite() or number < minimum or number > maximum:
        raise ValueError(f'Число должно быть от {minimum} до {maximum}.')
    return number


def parse_amount(text: str) -> int:
    amount = decimal_value(text)
    if amount != amount.quantize(Decimal('0.01')):
        raise ValueError('Допустимо не более двух знаков после запятой.')
    return int(amount * 100)


def money(minor: int, currency: str = 'RUB') -> str:
    amount = Decimal(minor) / 100
    rendered = f'{amount:,.0f}' if minor % 100 == 0 else f'{amount:,.2f}'
    return rendered.replace(',', ' ').replace('.', ',') + f' {currency}'


def ceil_minor(value: Decimal) -> int:
    return int(value.quantize(Decimal('1'), rounding=ROUND_CEILING))


def whole_number(value, *, minimum=0, maximum=10000000) -> int:
    number = decimal_value(value, minimum=Decimal(minimum), maximum=Decimal(maximum))
    if number != number.to_integral_value():
        raise ValueError('Введите целое число без копеек и дробной части.')
    return int(number)


def ceil_currency(minor: Decimal) -> int:
    return ceil_minor(minor / 100) * 100


def quote(base_rub_minor: int, method: str, settings: dict, country=None, crypto=None, *, stars_total=0) -> dict:
    if method not in {'stars', 'card', 'crypto'} or base_rub_minor <= 0:
        raise ValueError('Некорректный способ оплаты или сумма.')
    total = base_rub_minor + whole_number(settings.get('delivery_rub', '0')) * 100
    stars = whole_number(stars_total, maximum=1000000000)
    if method == 'stars' and not stars:
        raise ValueError('Цена Stars ещё не задана для всех товаров. Выберите другой способ оплаты.')
    stars = stars + whole_number(settings.get('delivery_stars', '0')) if stars else 0
    rate = Decimal('1')
    currency = 'RUB'
    if method == 'card' and country is not None:
        currency = country['card_currency']
        if currency != 'RUB':
            rate = decimal_value(country['rate_per_rub'], minimum=Decimal('0.00000001'))
    elif method == 'crypto':
        if crypto is None:
            raise ValueError('Выберите криптовалюту и сеть.')
        currency = crypto['token']
        rate = 1 / decimal_value(crypto['rub_per_unit'], minimum=Decimal('0.00000001'))
    return {
        'total_rub_minor': total,
        'total_stars': stars,
        'payable_minor': stars if method == 'stars' else ceil_currency(Decimal(total) * rate),
        'payable_currency': 'XTR' if method == 'stars' else currency,
        'rate': str(rate),
        'crypto_network': dict(crypto).get('network', '') if method == 'crypto' else '',
        'card_details_mode': ('shared' if settings.get('card_shared_details_enabled') == '1' else 'country') if method == 'card' else '',
        'delivery_rub_minor': total - base_rub_minor,
        'delivery_stars': whole_number(settings.get('delivery_stars', '0')),
    }


def legacy_rubles(stars: int) -> int:
    return max(1, int((Decimal(stars) * 1200 / Decimal('1.3')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))
