"""Classical ORB/geometric matching. No neural models, OCR or network calls."""
import base64
from functools import lru_cache

import cv2
import numpy as np

cv2.setNumThreads(1)


@lru_cache(maxsize=24)
def features(url):
    raw=base64.b64decode(url.split(',',1)[1],validate=True)
    image=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
    if image is None or min(image.shape)<64 or image.std()<12:
        return None
    if max(image.shape)!=960:
        scale=960/max(image.shape)
        image=cv2.resize(image,None,fx=scale,fy=scale,interpolation=cv2.INTER_AREA if scale<1 else cv2.INTER_CUBIC)
    points,descriptors=cv2.ORB_create(nfeatures=1800,fastThreshold=12).detectAndCompute(image,None)
    if descriptors is None or len(points)<35:
        return None
    return image,np.float32([p.pt for p in points]),descriptors


def same_artwork(first,second):
    """Require distributed geometric agreement and matching image appearance."""
    left=features(first);right=features(second)
    if left is None or right is None:return False
    a,pa,da=left;b,pb,db=right
    pairs=cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da,db,k=2)
    selected={}
    for pair in pairs:
        if len(pair)!=2:continue
        m,n=pair
        if m.distance<.72*n.distance and m.distance<60:
            previous=selected.get(m.trainIdx)
            if previous is None or m.distance<previous.distance:selected[m.trainIdx]=m
    matches=list(selected.values())
    if len(matches)<35:return False
    src=np.float32([pa[m.queryIdx] for m in matches]);dst=np.float32([pb[m.trainIdx] for m in matches])
    matrix,inliers=cv2.findHomography(src,dst,cv2.RANSAC,3.0,maxIters=1500,confidence=.995)
    if matrix is None or inliers is None or not np.isfinite(matrix).all():return False
    mask=inliers.ravel().astype(bool)
    if mask.sum()<30 or mask.mean()<.75:return False
    for points,image in ((src[mask],a),(dst[mask],b)):
        height,width=image.shape
        hull=cv2.contourArea(cv2.convexHull(points))
        if hull/(height*width)<.24:return False
        span=np.ptp(points,axis=0)
        if span[0]/width<.5 or span[1]/height<.45:return False
    warped=cv2.warpPerspective(a,matrix,(b.shape[1],b.shape[0]))
    valid=cv2.warpPerspective(np.ones_like(a,dtype=np.uint8),matrix,(b.shape[1],b.shape[0]))>0
    if valid.mean()<.45:return False
    x=cv2.GaussianBlur(warped,(5,5),0)[valid].astype(np.float32)
    y=cv2.GaussianBlur(b,(5,5),0)[valid].astype(np.float32)
    difference=np.abs(x-y-np.median(x-y))
    return bool(np.median(difference)<10 and (difference>35).mean()<.09)


def matching_artwork(actual,expected):
    try:
        return any(same_artwork(a['image_url'],e['image_url']) for a in actual for e in expected
                   if a.get('type')=='input_image' and e.get('type')=='input_image')
    except (ValueError,TypeError,cv2.error):
        return False


def matching_indices(actual,expected):
    return {index for index,part in enumerate(actual) if matching_artwork([part],expected)}
