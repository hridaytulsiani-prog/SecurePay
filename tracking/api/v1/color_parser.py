# K-means color-clustering used by validate.py's Delhivery color-fingerprint
# fallback (see DELHIVERY_FINGERPRINT_COLORS there) when a label's courier
# can't be identified from text/logo alone.
import pymupdf as fitz
import numpy as np
from PIL import Image


def _cluster_rgb_colors(pixels, n_clusters, random_state=42, n_init=10, max_iter=100):
    """Cluster RGB pixels using weighted, NumPy-only k-means.

    Working with unique colors makes this substantially cheaper than running
    k-means over every rendered pixel while producing the same weighted
    centroids and cluster sizes.  Keeping this implementation local also
    avoids importing scikit-learn's native DLLs during Django startup.
    """
    unique_pixels, pixel_counts = np.unique(pixels, axis=0, return_counts=True)
    points = unique_pixels.astype(np.float64)
    weights = pixel_counts.astype(np.float64)
    cluster_count = min(int(n_clusters), len(points))

    if cluster_count < 1:
        return np.empty((0, 3), dtype=np.float64), np.empty(0, dtype=np.int64)

    best_centers = None
    best_labels = None
    best_inertia = np.inf
    base_rng = np.random.RandomState(random_state)

    for _ in range(max(1, int(n_init))):
        rng = np.random.RandomState(base_rng.randint(0, np.iinfo(np.int32).max))

        # Weighted k-means++ initialization. Each unique RGB value represents
        # pixel_counts[i] original pixels.
        first_index = rng.choice(len(points), p=weights / weights.sum())
        centers = [points[first_index].copy()]
        closest_dist_sq = np.sum((points - centers[0]) ** 2, axis=1)

        for _cluster_index in range(1, cluster_count):
            probabilities = weights * closest_dist_sq
            probability_sum = probabilities.sum()
            if probability_sum <= 0:
                selected = {tuple(center) for center in centers}
                next_index = next(
                    index for index, point in enumerate(points) if tuple(point) not in selected
                )
            else:
                next_index = rng.choice(len(points), p=probabilities / probability_sum)
            centers.append(points[next_index].copy())
            new_dist_sq = np.sum((points - centers[-1]) ** 2, axis=1)
            closest_dist_sq = np.minimum(closest_dist_sq, new_dist_sq)

        centers = np.asarray(centers)
        for _iteration in range(max_iter):
            distances = np.sum((points[:, None, :] - centers[None, :, :]) ** 2, axis=2)
            labels = np.argmin(distances, axis=1)
            new_centers = centers.copy()

            for cluster_index in range(cluster_count):
                mask = labels == cluster_index
                if np.any(mask):
                    new_centers[cluster_index] = np.average(
                        points[mask], axis=0, weights=weights[mask]
                    )

            if np.allclose(new_centers, centers, atol=1e-3, rtol=0):
                centers = new_centers
                break
            centers = new_centers

        distances = np.sum((points[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        labels = np.argmin(distances, axis=1)
        inertia = np.sum(weights * distances[np.arange(len(points)), labels])
        if inertia < best_inertia:
            best_inertia = inertia
            best_centers = centers
            best_labels = labels

    cluster_sizes = np.bincount(
        best_labels, weights=pixel_counts, minlength=cluster_count
    ).astype(np.int64)
    nonempty = cluster_sizes > 0
    return best_centers[nonempty], cluster_sizes[nonempty]


def rgb_to_hex(rgb):
    return '#%02x%02x%02x' % tuple(int(x) for x in rgb)


def is_gray(pixel, tolerance=18):
    r, g, b = pixel
    return max(abs(int(r)-int(g)), abs(int(g)-int(b)), abs(int(r)-int(b))) < tolerance


def extract_major_colors_from_pdf(
    pdf_path,
    colors_per_page=8,
    max_pages=None,
    render_scale=2.0,
    resize_width=400,
    white_threshold=245,
    black_threshold=15,
    gray_tolerance=18,
    min_color_pixels=50
):
    doc = fitz.open(pdf_path)
    page_results = []
    combined_pixels = []

    total_pages = len(doc)
    pages_to_process = min(total_pages, max_pages) if max_pages else total_pages

    for page_index in range(pages_to_process):
        page = doc.load_page(page_index)
        matrix = fitz.Matrix(render_scale, render_scale)
        pix = page.get_pixmap(matrix=matrix, alpha=False)

        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        w, h = img.size
        new_height = int((resize_width / w) * h)
        img = img.resize((resize_width, new_height))

        arr = np.array(img).reshape(-1, 3)

        # remove near-white and near-black
        arr = arr[
            ~(
                ((arr[:, 0] > white_threshold) & (arr[:, 1] > white_threshold) & (arr[:, 2] > white_threshold)) |
                ((arr[:, 0] < black_threshold) & (arr[:, 1] < black_threshold) & (arr[:, 2] < black_threshold))
            )
        ]

        if len(arr) == 0:
            page_results.append({"page": page_index + 1, "colors": []})
            continue

        # split colorful and gray pixels
        colorful_pixels = np.array([p for p in arr if not is_gray(p, gray_tolerance)])
        gray_pixels = np.array([p for p in arr if is_gray(p, gray_tolerance)])

        # Prefer colorful pixels if enough exist
        if len(colorful_pixels) >= min_color_pixels:
            working_pixels = colorful_pixels
        else:
            working_pixels = arr

        combined_pixels.append(working_pixels)

        centers, counts = _cluster_rgb_colors(working_pixels, colors_per_page)
        total = counts.sum()

        colors = []
        for center, count in sorted(zip(centers, counts), key=lambda x: x[1], reverse=True):
            rgb = [int(c) for c in center]
            colors.append({
                "rgb": rgb,
                "hex": rgb_to_hex(rgb),
                "percentage": round(float(count / total) * 100, 2)
            })

        page_results.append({
            "page": page_index + 1,
            "colors": colors
        })

    combined_result = []
    if combined_pixels:
        merged = np.vstack(combined_pixels)
        centers, counts = _cluster_rgb_colors(merged, colors_per_page)
        total = counts.sum()

        for center, count in sorted(zip(centers, counts), key=lambda x: x[1], reverse=True):
            rgb = [int(c) for c in center]
            combined_result.append({
                "rgb": rgb,
                "hex": rgb_to_hex(rgb),
                "percentage": round(float(count / total) * 100, 2)
            })

    return {
        "all_pages": page_results,
        "combined_major_colors": combined_result
    }


# WARNING: this is a module-level script, not guarded by `if __name__ ==
# "__main__"`. It runs — against a hardcoded local file path — every single
# time this module is imported, which happens on every Django startup via
# validate.py's `from .color_parser import extract_major_colors_from_pdf`.
# That's the source of the "Page 1: {'rgb': ...}" noise printed on every
# `manage.py` invocation. Left as-is (not asked to fix), but worth knowing
# this isn't intentional logging — it's leftover ad-hoc test code.
# result = extract_major_colors_from_pdf("/home/rishabh/Desktop/SecurePayD/securepay/bluedart-2.pdf", colors_per_page=6)

# for page in result["all_pages"]:
#     print(f"Page {page['page']}:")
#     for color in page["colors"]:
#         print(color)

