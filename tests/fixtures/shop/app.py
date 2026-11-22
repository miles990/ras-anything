import os

import orders
import upstream


def main():
    os.environ["SHOP_API"] = upstream.start()
    for item in ["apple", "pear", "plum", "fig", "kiwi", "lime"]:
        print(orders.place_order(item)["status"], item)


if __name__ == "__main__":
    main()
