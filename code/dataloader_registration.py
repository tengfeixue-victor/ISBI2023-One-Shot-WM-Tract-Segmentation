"""
Code to load data and to create batches of 2D slices from 3D images.

Info:
Dimensions order for DeepLearningBatchGenerator: (batch_size, channels, x, y, [z])
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from os.path import join
import random

import numpy as np
import nibabel as nib

from batchgenerators.transforms.resample_transforms import ResampleTransform
from batchgenerators.transforms.resample_transforms import SimulateLowResolutionTransform
from batchgenerators.transforms.noise_transforms import GaussianNoiseTransform
from batchgenerators.transforms.noise_transforms import GaussianBlurTransform
from batchgenerators.transforms.spatial_transforms import SpatialTransform
from batchgenerators.transforms.spatial_transforms import ZoomTransform
from batchgenerators.transforms.spatial_transforms import MirrorTransform
from batchgenerators.transforms.utility_transforms import NumpyToTensor
from batchgenerators.transforms.abstract_transforms import Compose
from batchgenerators.dataloading.multi_threaded_augmenter import MultiThreadedAugmenter
from batchgenerators.dataloading.single_threaded_augmenter import SingleThreadedAugmenter
from batchgenerators.dataloading.data_loader import SlimDataLoaderBase
from batchgenerators.augmentations.utils import pad_nd_image
from batchgenerators.augmentations.utils import center_crop_2D_image_batched
from batchgenerators.augmentations.crop_and_pad_augmentations import crop
from batchgenerators.augmentations.spatial_transformations import augment_zoom

# from batchgenerators.transforms.sample_normalization_transforms import ZeroMeanUnitVarianceTransform
from tractseg.data.DLDABG_standalone import ZeroMeanUnitVarianceTransform as ZeroMeanUnitVarianceTransform_Standalone

from tractseg.data.custom_transformations import ResampleTransformLegacy
from tractseg.data.custom_transformations import FlipVectorAxisTransform
from tractseg.data.spatial_transform_peaks import SpatialTransformPeaks
from tractseg.data.spatial_transform_custom import SpatialTransformCustom
from tractseg.libs.system_config import SystemConfig as C
from tractseg.libs import data_utils
from tractseg.libs import peak_utils
import torch
from scipy import ndimage
import psutil
from joblib import Parallel, delayed


m_seed = 2022
np.random.seed(m_seed)


def peaks_to_tensors(peaks):
    """
    Convert peak image to tensor image

    Args:
        peaks: shape: [x,y,z,nr_peaks*3]

    Returns:
        tensor with shape: [x,y,z, nr_peaks*6]
    """

    def _peak_to_tensor(peak):
        tensor = np.zeros(peak.shape[:3] + (6,), dtype=np.float32)
        tensor[..., 0] = peak[..., 0] * peak[..., 0]
        tensor[..., 1] = peak[..., 0] * peak[..., 1]
        tensor[..., 2] = peak[..., 0] * peak[..., 2]
        tensor[..., 3] = peak[..., 1] * peak[..., 1]
        tensor[..., 4] = peak[..., 1] * peak[..., 2]
        tensor[..., 5] = peak[..., 2] * peak[..., 2]
        return tensor

    nr_peaks = int(peaks.shape[3] / 3)
    tensor = np.zeros(peaks.shape[:3] + (nr_peaks * 6,), dtype=np.float32)
    for idx in range(nr_peaks):
        tensor[..., idx * 6:(idx * 6) + 6] = _peak_to_tensor(peaks[..., idx * 3:(idx * 3) + 3])
    return tensor


def resize_first_three_dims(img, order=0, zoom=0.62, nr_cpus=1):
    def _process_gradient(grad_idx):
        return ndimage.zoom(img[:, :, :, grad_idx], zoom, order=order)

    nr_cpus = psutil.cpu_count() if nr_cpus == -1 else nr_cpus
    img_sm = Parallel(n_jobs=nr_cpus)(delayed(_process_gradient)(grad_idx) for grad_idx in range(img.shape[3]))

    return np.array(img_sm).transpose(1, 2, 3, 0)  # grads channel was in front -> put to back


def crop_is_pad_and_zoom(data, target_size=144):
    nr_dims = len(data.shape)
    assert (nr_dims <= 3), "image has to be like (145, 174, 145, 9)"
    shape = data.shape
    biggest_dim = max(shape)

    # Pad to make square
    new_img = np.zeros((biggest_dim, biggest_dim, biggest_dim, shape[3])).astype(data.dtype)

    pad1 = (biggest_dim - shape[0]) / 2.
    pad2 = (biggest_dim - shape[1]) / 2.
    pad3 = (biggest_dim - shape[2]) / 2.
    new_img[int(pad1):int(pad1) + shape[0],
    int(pad2):int(pad2) + shape[1],
    int(pad3):int(pad3) + shape[2]] = data

    # Scale to right size
    zoom = float(target_size) / biggest_dim

    # use order=0, otherwise does not work for peak images (results would be wrong)
    new_img = resize_first_three_dims(new_img, order=0, zoom=zoom)
    return new_img


def load_training_data(data_path, label_path, subject, tract_name):
    def load(filepath, nii_name):
        data = nib.load(join(filepath, nii_name + ".nii.gz"))
        data=data.get_fdata()
        # if nii_name=="peaks":
        #      data = peaks_to_tensors(data)
        data = np.nan_to_num(data)
        return data

    data = load(join(data_path, subject), "peaks")
    parts = tract_name
    seg = []  # [4, x, y, z, 72]
    for part in parts:
        seg.append(load(join(label_path, subject, "tracts"), part))
    seg = np.array(seg)
    seg = seg.transpose(1, 2, 3, 0)
    seg = seg.reshape(data.shape[:3] + (-1,))
    # print("before:",data.shape,seg.shape)

    return data, seg


class BatchGenerator2D_Nifti_random:
    def __init__(self, batch_size, data_dir, label_dir, subjects,subject_atlas, tract_name):
        self.batch_size = batch_size
        self.data_dir = data_dir
        self.label_dir = label_dir
        self.subjects = subjects
        self.tract_name = tract_name
        self.slice_direction_list = [0, 1, 2]
        self.subject_idx_list = list(range(len(self.subjects)))
        self.subject_idx = None
        self.subject_atlas = subject_atlas
    def __iter__(self):
        return self

    def __next__(self):
        return self.generate_train_batch()

    def generate_train_batch(self):

        # subject_idx = int(random.uniform(0, len(self.subjects)))
        if len(self.slice_direction_list) == 0 or self.subject_idx == None:
            if len(self.subject_idx_list) == 0:
                self.subject_idx_list = list(range(len(self.subjects)))
                subject_idx = np.random.choice(self.subject_idx_list, 1, False, None)
                subject_idx = int(subject_idx[0])
                self.subject_idx_list.remove(subject_idx)
            else:
                subject_idx = np.random.choice(self.subject_idx_list, 1, False, None)
                subject_idx = int(subject_idx[0])
                self.subject_idx_list.remove(subject_idx)
            self.subject_idx = subject_idx
        else:
            subject_idx = self.subject_idx
        data, seg = load_training_data(self.data_dir, self.label_dir, self.subjects[subject_idx], self.tract_name)
        atlas,atlas_label = load_training_data(self.data_dir, self.label_dir, self.subject_atlas, self.tract_name)

        if len(self.slice_direction_list) == 0:
            self.slice_direction_list = [0, 1, 2]
            slice_direction = np.random.choice(self.slice_direction_list, 1, False, None)
            # print("slice_list",self.slice_direction_list,"slice",slice_direction)
            slice_direction = int(slice_direction[0])
            self.slice_direction_list.remove(slice_direction)

        else:
            slice_direction = np.random.choice(self.slice_direction_list, 1, False, None)
            # print("slice_list",self.slice_direction_list,"slice",slice_direction)
            slice_direction = int(slice_direction[0])
            self.slice_direction_list.remove(slice_direction)

        if data.shape[slice_direction] <= self.batch_size:
            print("INFO: Batch size bigger than nr of slices. Therefore sampling with replacement.")
            slice_idxs = np.random.choice(data.shape[slice_direction], self.batch_size, True, None)
        else:
            slice_idxs = np.random.choice(data.shape[slice_direction], self.batch_size, False, None)
        x, y = data_utils.sample_slices(data, seg, slice_idxs, slice_direction=slice_direction,
                                        labels_type="int")
        x_atlas, y_atlas = data_utils.sample_slices(atlas, atlas_label, slice_idxs, slice_direction=slice_direction,
                                        labels_type="int")
        x, y = crop(x, y, 144)
        # Does not make it slower
        x = x.astype(np.float32)
        y = y.astype(np.float32)

        x_atlas, y_atlas = crop(x_atlas, y_atlas, 144)
        # Does not make it slower
        x_atlas = x_atlas.astype(np.float32)
        y_atlas = y_atlas.astype(np.float32)


        # possible optimization: sample slices from different patients and pad all to same size (size of biggest)
        data_dict = {"data": x,  # (batch_size, channels, x, y, [z])
                     "seg": y,
                     "atlas":x_atlas,
                     "atlas_label":y_atlas,
                     "subject_index": subject_idx,
                     "slice_dir": slice_direction}  # (batch_size, channels, x, y, [z])
        return data_dict


class BatchGenerator2D_data_ordered_standalone:
    """
    Creates batch of 2D slices from one subject.

    Does not depend on DKFZ/BatchGenerators package. Therefore good for inference on windows
    where DKFZ/Batchgenerators do not work (because of MultiThreading problems)
    """

    def __init__(self, batch_size, data_dir, label_dir, subjects, tract_name, subject_idx,subject_atlas):
        self.batch_size = batch_size
        self.data_dir = data_dir
        self.label_dir = label_dir
        self.subjects = subjects
        self.tract_name = tract_name
        self.global_idx = 0
        self.global_idx_end = 144
        self.subject_idx = subject_idx
        self.subject_atlas=subject_atlas
    def __iter__(self):
        return self

    def __next__(self):
        return self.generate_train_batch()

    def generate_train_batch(self):

        data, seg = load_training_data(self.data_dir, self.label_dir, self.subjects[self.subject_idx], self.tract_name)
        atlas, atlas_label = load_training_data(self.data_dir, self.label_dir, self.subject_atlas,
                                                self.tract_name)

        data = np.expand_dims(data.transpose(3, 0, 1, 2), axis=0)
        seg = np.expand_dims(seg.transpose(3, 0, 1, 2), axis=0)

        data, seg = crop(data, seg, 144)

        data = np.squeeze(data, axis=0).transpose(1, 2, 3, 0)
        seg = np.squeeze(seg, axis=0).transpose(1, 2, 3, 0)
        ####################################
        atlas = np.expand_dims(atlas.transpose(3, 0, 1, 2), axis=0)
        atlas_label = np.expand_dims(atlas_label.transpose(3, 0, 1, 2), axis=0)

        atlas, atlas_label = crop(atlas, atlas_label, 144)

        atlas = np.squeeze(atlas, axis=0).transpose(1, 2, 3, 0)
        atlas_label = np.squeeze(atlas_label, axis=0).transpose(1, 2, 3, 0)

        end = self.global_idx_end

        # Stop iterating if we reached end of data
        if self.global_idx >= end:
            self.global_idx = 0
            raise StopIteration

        new_global_idx = self.global_idx + self.batch_size

        # If we reach end, make last batch smaller, so it fits exactly for rest
        if new_global_idx >= end:
            new_global_idx = end  # not end-1, because this goes into range, and there automatically -1

        slice_idxs = list(range(self.global_idx, new_global_idx))

        data_x, seg_x = data_utils.sample_slices(data, seg, slice_idxs,
                                                 slice_direction=0,
                                                 labels_type='int')
        data_y, seg_y = data_utils.sample_slices(data, seg, slice_idxs,
                                                 slice_direction=1,
                                                 labels_type='int')
        data_z, seg_z = data_utils.sample_slices(data, seg, slice_idxs,
                                                 slice_direction=2,
                                                 labels_type='int')
        ####################################
        atlas_x, atlas_label_x = data_utils.sample_slices(atlas, atlas_label, slice_idxs,
                                                 slice_direction=0,
                                                 labels_type='int')
        atlas_y, atlas_label_y = data_utils.sample_slices(atlas, atlas_label, slice_idxs,
                                                 slice_direction=1,
                                                 labels_type='int')
        atlas_z, atlas_label_z = data_utils.sample_slices(atlas, atlas_label, slice_idxs,
                                                 slice_direction=2,
                                                 labels_type='int')

        # Does not make it slower
        data_x = data_x.astype(np.float32)
        seg_x = seg_x.astype(np.float32)
        data_y = data_y.astype(np.float32)
        seg_y = seg_y.astype(np.float32)
        data_z = data_z.astype(np.float32)
        seg_z = seg_z.astype(np.float32)
        data_dict = {"data_x": data_x,  # (batch_size, channels, x, y, [z])
                     "seg_x": seg_x,  # (batch_size, channels, x, y, [z])
                     "data_y": data_y,  # (batch_size, channels, x, y, [z])
                     "seg_y": seg_y,  # (batch_size, channels, x, y, [z])
                     "data_z": data_z,  # (batch_size, channels, x, y, [z])
                     "seg_z": seg_z,  # (batch_size, channels, x, y, [z])

                     "atlas_x": atlas_x,  # (batch_size, channels, x, y, [z])
                     "atlas_label_x": atlas_label_x,  # (batch_size, channels, x, y, [z])
                     "atlas_y": atlas_y,  # (batch_size, channels, x, y, [z])
                     "atlas_label_y": atlas_label_y,  # (batch_size, channels, x, y, [z])
                     "atlas_z": atlas_z,  # (batch_size, channels, x, y, [z])
                     "atlas_label_z": atlas_label_z,  # (batch_size, channels, x, y, [z])
                     }

        self.global_idx = new_global_idx
        return data_dict


class BatchGenerator3D_Nifti_random:
    def __init__(self, batch_size, data_dir, label_dir, subjects,subject_atlas, tract_name):
        self.batch_size = batch_size
        self.data_dir = data_dir
        self.label_dir = label_dir
        self.subjects = subjects
        self.tract_name = tract_name

        self.subject_idx_list = list(range(len(self.subjects)))
        self.subject_idx = None
        self.subject_atlas = subject_atlas
    def __iter__(self):
        return self

    def __next__(self):
        return self.generate_train_batch()

    def generate_train_batch(self):

        # subject_idx = int(random.uniform(0, len(self.subjects)))

        if len(self.subject_idx_list) == 0:
            self.subject_idx_list = list(range(len(self.subjects)))
            subject_idx = np.random.choice(self.subject_idx_list, 1, False, None)
            subject_idx = int(subject_idx[0])
            self.subject_idx_list.remove(subject_idx)
        else:
            subject_idx = np.random.choice(self.subject_idx_list, 1, False, None)
            subject_idx = int(subject_idx[0])
            self.subject_idx_list.remove(subject_idx)
        self.subject_idx = subject_idx

        moving_img, moving_label = load_training_data(self.data_dir, self.label_dir, self.subjects[subject_idx], self.tract_name)
        atlas_img,atlas_label = load_training_data(self.data_dir, self.label_dir, self.subject_atlas, self.tract_name)

        moving_img = moving_img.transpose(3, 0, 1, 2)  # channels have to be first
        moving_label = moving_label.transpose(3, 0, 1, 2)
        # Crop here instead of cropping entire batch at once to make each element in batch have same dimensions
        moving_img, moving_label = crop(moving_img[None, ...], moving_label[None, ...], crop_size=144)
        # moving_img = moving_img.squeeze(axis=0)
        # moving_label = moving_label.squeeze(axis=0)

        atlas_img = atlas_img.transpose(3, 0, 1, 2)  # channels have to be first
        atlas_label = atlas_label.transpose(3, 0, 1, 2)
        # Crop here instead of cropping entire batch at once to make each element in batch have same dimensions
        atlas_img, atlas_label = crop(atlas_img[None, ...], atlas_label[None, ...], crop_size=144)
        
        data_dict = {"moving_img": moving_img,  # (batch_size, channels, 144,144,144)
                     "moving_label": moving_label,
                     "atlas_img":atlas_img,
                     "atlas_label":atlas_label,
                     "subject_index": subject_idx,
                     }
        return data_dict


class BatchGenerator3D_data_ordered_standalone:
    def __init__(self, batch_size, data_dir, label_dir, subjects, subject_atlas,subject_idx, tract_name):
        self.batch_size = batch_size
        self.data_dir = data_dir
        self.label_dir = label_dir
        self.subjects = subjects
        self.tract_name = tract_name


        self.subject_idx = subject_idx
        self.subject_atlas = subject_atlas

    def __iter__(self):
        return self

    def __next__(self):
        return self.generate_train_batch()

    def generate_train_batch(self):


        subject_idx = self.subject_idx
        moving_img, moving_label = load_training_data(self.data_dir, self.label_dir, self.subjects[subject_idx],
                                                      self.tract_name)
        atlas_img, atlas_label = load_training_data(self.data_dir, self.label_dir, self.subject_atlas, self.tract_name)




        moving_img = moving_img.transpose(3, 0, 1, 2)  # channels have to be first
        moving_label = moving_label.transpose(3, 0, 1, 2)
        # Crop here instead of cropping entire batch at once to make each element in batch have same dimensions
        moving_img, moving_label = crop(moving_img[None, ...], moving_label[None, ...], crop_size=144)
    

        atlas_img = atlas_img.transpose(3, 0, 1, 2)  # channels have to be first
        atlas_label = atlas_label.transpose(3, 0, 1, 2)
        # Crop here instead of cropping entire batch at once to make each element in batch have same dimensions
        atlas_img, atlas_label = crop(atlas_img[None, ...], atlas_label[None, ...], crop_size=144)
       
        data_dict = {"moving_img": moving_img,  # (batch_size, 9, 144,144,144)
                     "moving_label": moving_label,
                     "atlas_img": atlas_img,
                     "atlas_label": atlas_label,
                     "subject_index": subject_idx,
                     }
        return data_dict

class DataLoaderTraining:

    def __init__(self, args, tract_name):
        self.args = args
        self.data_dir = args.data_dir
        self.label_dir = args.label_dir

        self.batch_size = args.batch_size
        self.tract_name = tract_name

    def get_batch_generator(self, subjects, fixed_subject, subject_idx=0, type="train"):


        if type == "train":
            batch_gen = BatchGenerator3D_Nifti_random(self.batch_size, self.data_dir, self.label_dir, subjects,fixed_subject,self.tract_name)
            batch_gen = self._augment_data(batch_gen, keys=["moving_img", "moving_label", "atlas_img","atlas_label",],)

        else:
            batch_gen = BatchGenerator3D_data_ordered_standalone(self.batch_size, self.data_dir, self.label_dir, subjects, fixed_subject, subject_idx, self.tract_name)
            batch_gen = self._augment_data(batch_gen, keys=["moving_img", "moving_label", "atlas_img","atlas_label",], )

        return batch_gen

    def _augment_data(self, batch_generator, keys):

        num_processes = 1

        tfs = []

        tfs.append(NumpyToTensor(keys=keys, cast_to="float"))
        batch_gen = SingleThreadedAugmenter(batch_generator, Compose(tfs))
        return batch_gen
