from re import template
from statistics import mode
from unittest import result
import numpy as np
import cv2
import math
import mediapipe as mp
from PIL import Image
import os
import csv

# https://github.com/liruilong940607/Pose2Seg/blob/master/lib/transforms.py

def get_affine_matrix(center, angle, translate, scale, shear=0):
    # Helper method to compute affine transformation

    # As it is explained in PIL.Image.rotate
    # We need compute affine transformation matrix: M = T * C * RSS * C^-1
    # where T is translation matrix: [1, 0, tx | 0, 1, ty | 0, 0, 1]
    #       C is translation matrix to keep center: [1, 0, cx | 0, 1, cy | 0, 0, 1]
    #       RSS is rotation with scale and shear matrix
    #       RSS(a, scale, shear) = [ cos(a)*sx    -sin(a + shear)*sy     0]
    #                              [ sin(a)*sx    cos(a + shear)*sy     0]
    #                              [     0                  0          1]

    angle = math.radians(angle)
    shear = math.radians(shear)

    T = np.array([[1, 0, translate[0]], [0, 1, translate[1]], [0, 0, 1]]).astype(np.float32)
    C = np.array([[1, 0, center[0]], [0, 1, center[1]], [0, 0, 1]]).astype(np.float32)
    RSS = np.array([[ math.cos(angle)*scale[0], -math.sin(angle + shear)*scale[1], 0],
                    [ math.sin(angle)*scale[0],  math.cos(angle + shear)*scale[1], 0],
                    [ 0, 0, 1]]).astype(np.float32)
    C_inv = np.linalg.inv(np.mat(C))
    M = T.dot(C).dot(RSS).dot(C_inv)
    return M

def warpAffinePoints(pts, H):
    # pts: (N, (x,y))
    pts = np.array(pts, dtype=np.float32)
    assert H.shape in [(3,3), (2,3)], 'H.shape must be (2,3) or (3,3): {}'.format(H.shape)
    ext = np.ones((len(pts), 1), dtype=pts.dtype)
    return np.array(np.hstack((pts, ext)).dot(H[0:2, :].transpose(1, 0)), dtype=np.float32)

def get_resize_padding_matrix(srcW, srcH, dstW, dstH, iscenter=False):
    # this function keep ratio
    scalex = scaley = min(float(dstW)/srcW, float(dstH)/srcH)
    if iscenter:
        translate = ((dstW - srcW * scalex)/2.0, (dstH - srcH * scaley)/2.0)
    else:
        translate = (0, 0)
    return get_affine_matrix(center=(0, 0), angle=0, translate=translate, scale=(scalex, scaley))

def get_resize_matrix(srcW, srcH, dstW, dstH):
    # this function do not keep ratio
    scalex, scaley = (float(dstW)/srcW, float(dstH)/srcH)
    return get_affine_matrix(center=(0, 0), angle=0, translate=(0, 0), scale=(scalex, scaley))



# https://github.com/liruilong940607/Pose2Seg/blob/64fcc5e0ee7b85c32f4be2771ce810a41b9fcb38/modeling/core.py#L159
def pose_affinematrix(src_kpt, dst_kpt, dst_area, hard=False):
    ''' `dst_kpt` is the template. 
    Args:
        src_kpt, dst_kpt: (17, 3)
        dst_area: used to uniform returned score.
        hard: 
            - True: for `dst_kpt` is the template. we do not want src_kpt
                to match a template and out of range. So in this case, 
                src_kpt[vis] should convered by dst_kpt[vis]. if not, will 
                return score = 0
            - False: for matching two kpts.
    Returns:
        matrix: (2, 3)
        score: align confidence/similarity, a float between 0 and 1.
    '''
    # set confidence constriants
    src_vis = src_kpt[:, 2] > 0
    dst_vis = dst_kpt[:, 2] > 0
    visI = np.logical_and(src_vis, dst_vis)
    visU = np.logical_or(src_vis, dst_vis)
    # - 0 Intersection Points means we know nothing to calc matrix.
    # - 1 Intersection Points means there are infinite matrix.
    # - 2 Intersection Points means there are 2 possible matrix.
    #   But in most case, it will lead to a really bad solution
    if sum(visI) == 0 or sum(visI) == 1 or sum(visI) == 2:
        matrix = np.array([[1, 0, 0], 
                           [0, 1, 0]], dtype=np.float32)
        score = 0.
        return matrix, score
    
    if hard and (False in dst_vis[src_vis]):
        matrix = np.array([[1, 0, 0], 
                           [0, 1, 0]], dtype=np.float32)
        score = 0.
        return matrix, score
      
    src_valid = src_kpt[visI, 0:2]
    dst_valid = dst_kpt[visI, 0:2]
    matrix = solve_affinematrix(src_valid, dst_valid, fullAffine=False)
    matrix = np.vstack((matrix, np.array([0,0,1], dtype=np.float32)))
    
    # calc score
    #sigmas = np.array([.26, .25, .25, .35, .35, .79, .79, .72, .72, .62,.62, 1.07, 1.07, .87, .87, .89, .89])/10.0
    #vars_valid = ((sigmas * 2)**2)[visI]
    vars_valid = 1
    diff = warpAffinePoints(src_valid, matrix) - dst_valid
    error = np.sum(diff**2, axis=1) / vars_valid / dst_area / 2
    score = np.mean(np.exp(-error)) * np.sum(visI) / np.sum(visU)
    
    return matrix, score

def solve_affinematrix(src, dst):
    '''
    Document: https://docs.opencv.org/2.4/modules/core/doc/operations_on_arrays.html?highlight=solve#cv2.solve
    C++ Version: aff_trans.cpp in opencv
    src: numpy array (N, 2)
    dst: numpy array (N, 2)
    fullAffine = False means affine align without shear.
    '''
    src = src.reshape(-1, 1, 2)
    dst = dst.reshape(-1, 1, 2)
    
    out = np.zeros((2,3), np.float32)
    siz = 2*src.shape[0]

    matM = np.zeros((siz,4), np.float32)
    matP = np.zeros((siz,1), np.float32)
    contPt=0
    for ii in range(0, siz):
        therow = np.zeros((1,4), np.float32)
        if ii%2==0:
            therow[0,0] = src[contPt, 0, 0] # x
            therow[0,1] = src[contPt, 0, 1] # y
            therow[0,2] = 1
            matM[ii,:] = therow[0,:].copy()
            matP[ii,0] = dst[contPt, 0, 0] # x
        else:
            therow[0,0] = src[contPt, 0, 1] # y ## Notice, c++ version is - here
            therow[0,1] = -src[contPt, 0, 0] # x
            therow[0,3] = 1
            matM[ii,:] = therow[0,:].copy()
            matP[ii,0] = dst[contPt, 0, 1] # y
            contPt += 1
    sol = cv2.solve(matM, matP, flags = cv2.DECOMP_SVD)
    sol = sol[1]
    out[0,0]=sol[0,0]
    out[0,1]=sol[1,0]
    out[0,2]=sol[2,0]
    out[1,0]=-sol[1,0]
    out[1,1]=sol[0,0]
    out[1,2]=sol[3,0]

    # result
    return out


# for pose, pose_category in zip(self.templates, self.templates_category):
#             matrix, score = pose_affinematrix(kpt, pose, dst_area=1.0, hard=True)
#             if score > 0:
#                 # valid `matrix`. default (dstH, dstW) is (1.0, 1.0)
#                 matrix = get_resize_matrix(1.0, 1.0, dstW, dstH).dot(matrix)
#                 scale = math.sqrt(matrix[0,0] ** 2 + matrix[0,1] ** 2)
#                 category = pose_category
#             else:
#                 matrix = basic_matrix
#                 category = -1




mp_drawing = mp.solutions.drawing_utils
mp_drawing_styles = mp.solutions.drawing_styles
mp_hands = mp.solutions.hands

# For static images:
log = open('pose_log.txt', mode='w')
log.write('keep the landmark information from pose_alignment.py\n')
log.close()

five_input = os.path.join(os.getcwd(), 'templates', 'five_input.jpeg')
five_temp = os.path.join(os.getcwd(), 'templates','five_temp.jpeg')
IMAGE_FILES = [five_temp]
with mp_hands.Hands(
    static_image_mode=True,
    max_num_hands=2,
    min_detection_confidence=0.5) as hands:
  for idx, file in enumerate(IMAGE_FILES):
    # Read an image, flip it around y-axis for correct handedness output (see
    # above).
    image = cv2.flip(cv2.imread(file), 1)
    # Convert the BGR image to RGB before processing.
    results = hands.process(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))

    # Print handedness and draw hand landmarks on the image.
    log = open('pose_log.txt', 'a')
    log.write(file + '\n')
    log.write(str(results.multi_handedness))
    print(type(results.multi_handedness))

    if not results.multi_hand_landmarks:
      continue
    image_height, image_width, _ = image.shape
    annotated_image = image.copy()
    for hand_landmarks in results.multi_hand_landmarks:
      log.write(str(hand_landmarks))
      log.writelines(
          ['Index finger tip coordinates: (',
          str(hand_landmarks.landmark[mp_hands.HandLandmark.INDEX_FINGER_TIP].x * image_width), 
          str(hand_landmarks.landmark[mp_hands.HandLandmark.INDEX_FINGER_TIP].y * image_height),')']
      )
      log.close()
      mp_drawing.draw_landmarks(
          annotated_image,
          hand_landmarks,
          mp_hands.HAND_CONNECTIONS,
          mp_drawing_styles.get_default_hand_landmarks_style(),
          mp_drawing_styles.get_default_hand_connections_style())
    cv2.imwrite(
        '/tmp/annotated_image' + str(idx) + '.png', cv2.flip(annotated_image, 1))
    # Draw hand world landmarks.
    # if not results.multi_hand_world_landmarks:
    #   continue
    # for hand_landmarks in results.multi_hand_landmarks:
    #   mp_drawing.plot_landmarks(
    #     hand_landmarks, mp_hands.HAND_CONNECTIONS, azimuth=5)
    
    # my_landmark = np.array(hand_landmarks.landmark)
    # print(my_landmark[0].items())
    
    # put hand landmarks into numpy arrays for calculation
    temp = []
    for point in hand_landmarks.landmark:
        temp.append([point.x,point.y])
    print(len(temp))
    
    with open('temp.csv','w') as f:
        write = csv.writer(f)
        write.writerows(temp)


