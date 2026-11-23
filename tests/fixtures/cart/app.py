import os

import cart
import upstream


def main():
    os.environ["CART_API"] = upstream.start()
    for item in ["apple", "pear", "plum", "fig"]:
        print(cart.checkout(item)["status"], item)


if __name__ == "__main__":
    main()
