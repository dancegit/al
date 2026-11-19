from pkg.core import total


def average(prices):
    return total(prices) / len(prices) if prices else 0
