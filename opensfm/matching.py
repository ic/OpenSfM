import numpy as np
import cv2
import pyopengv
import logging

from opensfm import csfm
from opensfm import context
from opensfm import multiview


logger = logging.getLogger(__name__)


def match_words(f1, words1, f2, words2, config):
    """Match using words and apply Lowe's ratio filter.

    Args:
        f1: feature descriptors of the first image
        w1: the nth closest words for each feature in the first image
        f2: feature descriptors of the second image
        w2: the nth closest words for each feature in the second image
        config: config parameters
    """
    ratio = config['lowes_ratio']
    num_checks = config['bow_num_checks']
    return csfm.match_using_words(f1, words1, f2, words2[:, 0],
                                  ratio, num_checks)


def match_words_symmetric(f1, words1, f2, words2, config):
    """Match using words in both directions and keep consistent matches.

    Args:
        f1: feature descriptors of the first image
        w1: the nth closest words for each feature in the first image
        f2: feature descriptors of the second image
        w2: the nth closest words for each feature in the second image
        config: config parameters
    """
    matches_ij = match_words(f1, words1, f2, words2, config)
    matches_ji = match_words(f2, words2, f1, words1, config)
    matches_ij = [(a, b) for a, b in matches_ij]
    matches_ji = [(b, a) for a, b in matches_ji]

    matches = set(matches_ij).intersection(set(matches_ji))
    return np.array(list(matches), dtype=int)


def match_flann(index, f2, config):
    """Match using FLANN and apply Lowe's ratio filter.

    Args:
        index: flann index if the first image
        f2: feature descriptors of the second image
        config: config parameters
    """
    search_params = dict(checks=config['flann_checks'])
    results, dists = index.knnSearch(f2, 2, params=search_params)
    squared_ratio = config['lowes_ratio']**2  # Flann returns squared L2 distances
    good = dists[:, 0] < squared_ratio * dists[:, 1]
    matches = list(zip(results[good, 0], good.nonzero()[0]))
    return np.array(matches, dtype=int)


def match_flann_symmetric(fi, indexi, fj, indexj, config):
    """Match using FLANN in both directions and keep consistent matches.

    Args:
        fi: feature descriptors of the first image
        indexi: flann index if the first image
        fj: feature descriptors of the second image
        indexj: flann index of the second image
        config: config parameters
    """
    matches_ij = [(a, b) for a, b in match_flann(indexi, fj, config)]
    matches_ji = [(b, a) for a, b in match_flann(indexj, fi, config)]

    matches = set(matches_ij).intersection(set(matches_ji))
    return np.array(list(matches), dtype=int)


def match_brute_force(f1, f2, config):
    """Brute force matching and Lowe's ratio filtering.

    Args:
        f1: feature descriptors of the first image
        f2: feature descriptors of the second image
        config: config parameters
    """
    assert(f1.dtype.type == f2.dtype.type)
    if (f1.dtype.type == np.uint8):
        matcher_type = 'BruteForce-Hamming'
    else:
        matcher_type = 'BruteForce'
    matcher = cv2.DescriptorMatcher_create(matcher_type)
    matches = matcher.knnMatch(f1, f2, k=2)

    ratio = config['lowes_ratio']
    good_matches = []
    for match in matches:
        if match and len(match) == 2:
            m, n = match
            if m.distance < ratio * n.distance:
                good_matches.append(m)
    good_matches = _convert_matches_to_vector(good_matches)
    return np.array(good_matches, dtype=int)


def _convert_matches_to_vector(matches):
    """Convert Dmatch object to matrix form."""
    matches_vector = np.zeros((len(matches), 2), dtype=np.int)
    k = 0
    for mm in matches:
        matches_vector[k, 0] = mm.queryIdx
        matches_vector[k, 1] = mm.trainIdx
        k = k + 1
    return matches_vector


def match_brute_force_symmetric(fi, fj, config):
    """Match with brute force in both directions and keep consistent matches.

    Args:
        fi: feature descriptors of the first image
        fj: feature descriptors of the second image
        config: config parameters
    """
    matches_ij = [(a, b) for a, b in match_brute_force(fi, fj, config)]
    matches_ji = [(b, a) for a, b in match_brute_force(fj, fi, config)]

    matches = set(matches_ij).intersection(set(matches_ji))
    return np.array(list(matches), dtype=int)


def robust_match_fundamental(p1, p2, matches, config):
    """Filter matches by estimating the Fundamental matrix via RANSAC."""
    if len(matches) < 8:
        return None, np.array([])

    p1 = p1[matches[:, 0]][:, :2].copy()
    p2 = p2[matches[:, 1]][:, :2].copy()

    FM_RANSAC = cv2.FM_RANSAC if context.OPENCV3 else cv2.cv.CV_FM_RANSAC
    threshold = config['robust_matching_threshold']
    F, mask = cv2.findFundamentalMat(p1, p2, FM_RANSAC, threshold, 0.9999)
    inliers = mask.ravel().nonzero()

    if F is None or F[2, 2] == 0.0:
        return F, []

    return F, matches[inliers]


def _compute_inliers_bearings(b1, b2, T, threshold=0.01):
    R = T[:, :3]
    t = T[:, 3]
    p = pyopengv.triangulation_triangulate(b1, b2, t, R)

    br1 = p.copy()
    br1 /= np.linalg.norm(br1, axis=1)[:, np.newaxis]

    br2 = R.T.dot((p - t).T).T
    br2 /= np.linalg.norm(br2, axis=1)[:, np.newaxis]

    ok1 = multiview.vector_angle_many(br1, b1) < threshold
    ok2 = multiview.vector_angle_many(br2, b2) < threshold
    return ok1 * ok2


def robust_match_calibrated(p1, p2, camera1, camera2, matches, config):
    """Filter matches by estimating the Essential matrix via RANSAC."""

    if len(matches) < 8:
        return np.array([])

    p1 = p1[matches[:, 0]][:, :2].copy()
    p2 = p2[matches[:, 1]][:, :2].copy()
    b1 = camera1.pixel_bearing_many(p1)
    b2 = camera2.pixel_bearing_many(p2)

    threshold = config['robust_matching_calib_threshold']
    T = multiview.relative_pose_ransac(
        b1, b2, b"STEWENIUS", 1 - np.cos(threshold), 1000, 0.999)

    for relax in [4, 2, 1]:
        inliers = _compute_inliers_bearings(b1, b2, T, relax * threshold)
        if sum(inliers) < 8:
            return np.array([])
        T = pyopengv.relative_pose_optimize_nonlinear(
            b1[inliers], b2[inliers], T[:3, 3], T[:3, :3])

    inliers = _compute_inliers_bearings(b1, b2, T, threshold)

    return matches[inliers]


def robust_match(p1, p2, camera1, camera2, matches, config):
    """Filter matches by fitting a geometric model.

    If cameras are perspective without distortion, then the Fundamental
    matrix is used.  Otherwise, we use the Essential matrix.
    """
    if (camera1.projection_type == 'perspective'
            and camera1.k1 == 0.0 and camera1.k2 == 0.0
            and camera2.projection_type == 'perspective'
            and camera2.k1 == 0.0 and camera2.k2 == 0.0):
        return robust_match_fundamental(p1, p2, matches, config)[1]
    else:
        return robust_match_calibrated(p1, p2, camera1, camera2, matches, config)
