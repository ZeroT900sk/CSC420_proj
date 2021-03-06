"""
Author: Haoping Xu
adapted from https://github.com/JiaRenChang/PSMNet for
"Pyramid Stereo Matching Network" paper (CVPR 2018) by Jia-Ren Chang and Yong-Sheng Chen.
"""
import torch.utils.data as data
import torch
import torchvision.transforms as transforms
import random
from PIL import Image, ImageOps
import numpy as np
import os

__imagenet_stats = {'mean': [0.485, 0.456, 0.406],
                    'std': [0.229, 0.224, 0.225]}

__box_mean = [3.996132075471698908e+00,
              1.617452830188679469e+00,
              1.517264150943395506e+00]

IMG_EXTENSIONS = [
    '.jpg', '.JPG', '.jpeg', '.JPEG',
    '.png', '.PNG', '.ppm', '.PPM', '.bmp', '.BMP',
]


def scale_crop(input_size, scale_size=None, normalize=__imagenet_stats):
    t_list = [
        transforms.ToTensor(),
        transforms.Normalize(**normalize),
    ]

    return transforms.Compose(t_list)


def get_transform(name='imagenet', input_size=None,
                  scale_size=None, normalize=None):
    normalize = __imagenet_stats
    input_size = 256
    return scale_crop(input_size=input_size,
                      scale_size=scale_size, normalize=normalize)


def is_image_file(filename):
    return any(filename.endswith(extension) for extension in IMG_EXTENSIONS)


def default_loader(path):
    return Image.open(path).convert('RGB')


def disparity_loader(path):
    return Image.open(path)


def point_loader(path):
    return np.load(path).item()


def rotate_pc_along_y(pc, rot_angle):
    '''
    Input:
        pc: numpy array (N,C), first 3 channels are XYZ
            z is facing forward, x is left ward, y is downward
        rot_angle: rad scalar
    Output:
        pc: updated pc with XYZ rotated
    '''
    cosval = np.cos(rot_angle)
    sinval = np.sin(rot_angle)
    rotmat = np.array([[cosval, -sinval], [sinval, cosval]])
    pc[:, [0, 2]] = np.dot(pc[:, [0, 2]], np.transpose(rotmat))
    return pc


def angle2class(angle, num_class):
    ''' Convert continuous angle to discrete class and residual.
    Input:
        angle: rad scalar, from 0-2pi (or -pi~pi), class center at
            0, 1*(2pi/N), 2*(2pi/N) ...  (N-1)*(2pi/N)
        num_class: int scalar, number of classes N
    Output:
        class_id, int, among 0,1,...,N-1
        residual_angle: float, a number such that
            class*(2pi/N) + residual_angle = angle
    '''
    angle = angle % (2 * np.pi)
    assert (angle >= 0 and angle <= 2 * np.pi)
    angle_per_class = 2 * np.pi / float(num_class)
    shifted_angle = (angle + angle_per_class / 2) % (2 * np.pi)
    class_id = int(shifted_angle / angle_per_class)
    residual_angle = shifted_angle - \
                     (class_id * angle_per_class + angle_per_class / 2)
    return class_id, residual_angle



def size2class(size):
    ''' Convert 3D bounding box size to template class and residuals.
    todo (rqi): support multiple size clusters per type.

    Input:
        size: numpy array of shape (3,) for (l,w,h)
    Output:
        size_residual: numpy array of shape (3,)
    '''
    size_residual = size - __box_mean
    return size_residual



class myPointData(data.Dataset):
    def __init__(self, points_dir, num_point, num_angle, random_flip=False, random_shift=False, lidar=False):
        self.points = [points_dir + point for point in os.listdir(points_dir)]
        self.points = self.points
        self.num_point = num_point
        self.num_angle = num_angle
        self.random_flip = random_flip
        self.random_shift = random_shift
        self.lidar = lidar

    def __getitem__(self, index):
        point = self.points[index]
        datas = point_loader(point)
        img_id = datas['img_id']
        rot_angle = np.pi / 2 + datas['frustum_angle']
        if self.lidar:
            points = datas['point_velo']
            seg_mask = datas['velo_label']
        else:
            points = datas['point_2d']
            seg_mask = datas['label']

        # sampling n points from whole point cloud
        choice = np.random.choice(points.shape[0], self.num_point, replace=True)
        points = points[choice, :]
        # find the mask label for 3d points
        seg_mask = seg_mask[choice]

        # get 3d box center
        box3d_center = datas['box3d_center']

        head = datas['heading']

        # Data Augmentation
        if self.random_flip:
            # note: rot_angle won't be correct if we have random_flip
            # so do not use it in case of random flipping.
            if np.random.random() > 0.5:  # 50% chance flipping
                points[:, 0] *= -1
                box3d_center[0] *= -1
                head = np.pi - head

        if self.random_shift:
            dist = np.sqrt(np.sum(box3d_center[0] ** 2 + box3d_center[1] ** 2))
            shift = np.clip(np.random.randn() * dist * 0.05, dist * 0.8, dist * 1.2)
            points[:, 2] += shift
            box3d_center[2] += shift

        # convert 3d box size to mean + residual
        size_r = size2class(datas['box3d_size'])

        # rotate points and boxes to center of frustum
        points_rot = rotate_pc_along_y(points.copy(), rot_angle)
        box3d_center_rot = rotate_pc_along_y(np.expand_dims(box3d_center.copy(), 0), rot_angle).squeeze()
        angle_c_rot, angle_r_rot = angle2class(head - rot_angle, self.num_angle)

        return torch.FloatTensor(points_rot), \
               torch.LongTensor(seg_mask), \
               torch.FloatTensor(box3d_center_rot), \
               angle_c_rot, \
               angle_r_rot, \
               torch.FloatTensor(size_r), \
               rot_angle, \
               img_id, \
               point.split('/')[-1]

    def __len__(self):
        return len(self.points)


class myImageFloder(data.Dataset):
    def __init__(self, left, right, left_disparity, training, name, load=False, loader=default_loader,
                 dploader=disparity_loader):

        self.left = left
        self.right = right
        self.disp_L = left_disparity
        self.loader = loader
        self.dploader = dploader
        self.training = training
        self.name = name
        self.load = load

    def __getitem__(self, index):
        left = self.left[index]
        right = self.right[index]

        name = self.name[index]

        left_img = self.loader(left)
        right_img = self.loader(right)

        if not self.load:
            disp_L = self.disp_L[index]
            dataL = self.dploader(disp_L)

        if self.training:
            w, h = left_img.size
            th, tw = 256, 512

            x1 = random.randint(0, w - tw)
            y1 = random.randint(0, h - th)

            left_img = left_img.crop((x1, y1, x1 + tw, y1 + th))
            right_img = right_img.crop((x1, y1, x1 + tw, y1 + th))

            if not self.load:
                dataL = np.ascontiguousarray(dataL, dtype=np.float32) / 256
                dataL = dataL[y1:y1 + th, x1:x1 + tw]
            else:
                dataL = 0
            processed = get_transform()
            left_img = processed(left_img)
            right_img = processed(right_img)

            return left_img, right_img, dataL, name
        else:
            w, h = left_img.size

            left_img = left_img.crop((w - 1232, h - 368, w, h))
            right_img = right_img.crop((w - 1232, h - 368, w, h))
            w1, h1 = left_img.size
            if not self.load:
                dataL = dataL.crop((w - 1232, h - 368, w, h))
                dataL = np.ascontiguousarray(dataL, dtype=np.float32) / 256
            else:
                dataL = 0

            processed = get_transform()
            left_img = processed(left_img)
            right_img = processed(right_img)

            return left_img, right_img, dataL, name, w, h

    def __len__(self):
        return len(self.left)
