package shipping;

import java.util.List;

/** The evening shipping run: rate every parcel, book it with the carrier, print its label line. */
public final class Main {
    private Main() {}

    private static final List<Parcel> PARCELS = List.of(
            new Parcel("ORD-5001", "8001", 1200), new Parcel("ORD-5002", "1010", 350), new Parcel("ORD-5003", "75011", 4800),
            new Parcel("ORD-5004", "10115", 900), new Parcel("ORD-5005", "28013", 2300), new Parcel("ORD-5006", "00184", 150));

    public static void main(String[] args) throws Exception {
        try (FakeCarrier carrier = new FakeCarrier()) {
            int total = 0;
            for (Parcel p : PARCELS) {
                Rate rate = Carrier.fetchRate(carrier.url(), p);
                Booking booking = Carrier.bookShipment(carrier.url(), p, rate);
                String code = Labels.labelCode(booking.tracking());
                total += rate.cents();
                System.out.printf("%s %-7s %5d c  %s  label %s%n", p.order(), rate.service(), rate.cents(), booking.tracking(), code);
            }
            System.out.printf("%d parcels, %d c%n", PARCELS.size(), total);
        }
    }
}
