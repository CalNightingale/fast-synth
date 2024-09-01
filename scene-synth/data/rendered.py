"""
Handles pre-rendered rooms generated by data.top_down
RenderedScene loads the pre-rendered data
along with other precomputed information,
and creates RenderedComposite, which combines those to 
create the multi-channel top-down view used in the pipeline
"""

from torch.utils import data
from data import ObjectCategories, House, Obj
import random
import numpy as np
import math
import pickle
import os
import json
import copy
import torch
import utils


class RenderedScene:
    """
    Loading a rendered room
    Attributes
    ----------
    category_map (ObjectCategories): object category mapping
        that should be the same across all instances of the class
    categories (list[string]): all categories present in this room type.
        Loaded once when the first room is loaded to reduce disk access.
    cat_to_index (dict[string, int]): maps a category to corresponding index
    current_data_dir (string): keep track of the current data directory, if
        it changes, then categories and cat_to_index should be recomputed
    """

    category_map = ObjectCategories()
    categories = None
    cat_to_index = None
    current_data_dir = None

    def __init__(
        self,
        index,
        data_dir,
        data_root_dir=None,
        shuffle=True,
        load_objects=True,
        seed=None,
        rotation=0,
    ):
        """
        Load a rendered scene from file
        Parameters
        ----------
        index (int): room number
        data_dir (string): location of the pre-rendered rooms
        data_root_dir (string or None, optional): if specified,
            use this as the root directory
        shuffle (bool, optional): If true, randomly order the objects
            in the room. Otherwise use the default order as written
            in the original dataset
        load_objects (bool, optional): If false, only load the doors
            and windows. Otherwise load all objects in the room
        seed (int or None, optional): if set, use a fixed random seed
            so we can replicate a particular experiment
        """
        if seed:
            random.seed(seed)

        if not data_root_dir:
            data_root_dir = utils.get_data_root_dir()

        if (
            RenderedScene.categories is None
            or RenderedScene.current_data_dir != data_dir
        ):
            with open(
                f"{data_root_dir}/{data_dir}/final_categories_frequency", "r"
            ) as f:
                lines = f.readlines()
                cats = [line.split()[0] for line in lines]

            RenderedScene.categories = [
                cat for cat in cats if cat not in set(["window", "door"])
            ]
            RenderedScene.cat_to_index = {
                RenderedScene.categories[i]: i
                for i in range(len(RenderedScene.categories))
            }
            RenderedScene.current_data_dir = data_dir

        # print(index, rotation)
        if rotation != 0:
            fname = f"{index}_{rotation}"
        else:
            fname = index

        with open(f"{data_root_dir}/{data_dir}/{fname}.pkl", "rb") as f:
            (self.floor, self.wall, nodes), self.room = pickle.load(f)

        self.index = index
        self.rotation = rotation

        self.object_nodes = []
        self.door_window_nodes = []
        for node in nodes:
            category = RenderedScene.category_map.get_final_category(node["modelId"])
            if category in ["door", "window"]:
                node["category"] = category
                self.door_window_nodes.append(node)
            elif load_objects:
                node["category"] = RenderedScene.cat_to_index[category]
                self.object_nodes.append(node)

        if shuffle:
            random.shuffle(self.object_nodes)

        self.size = self.floor.shape[0]

    def create_composite(self):
        """
        Create a initial composite that only contains the floor,
        wall, doors and windows. See RenderedComposite for how
        to add more objects
        """
        r = RenderedComposite(
            RenderedScene.categories, self.floor, self.wall, self.door_window_nodes
        )
        return r


class RenderedComposite:
    """
    Multi-channel top-down composite, used as input to NN
    """

    def __init__(self, categories, floor, wall, door_window_nodes=None):
        # Optional door_window just in case
        self.size = floor.shape[0]

        self.categories = categories

        self.room_mask = floor + wall
        self.room_mask[self.room_mask != 0] = 1

        self.wall_mask = wall.clone()
        self.wall_mask[self.wall_mask != 0] = 0.5

        self.height_map = torch.max(floor, wall)
        self.cat_map = torch.zeros((len(self.categories), self.size, self.size))

        self.sin_map = torch.zeros((self.size, self.size))
        self.cos_map = torch.zeros((self.size, self.size))

        self.door_map = torch.zeros((self.size, self.size))
        self.window_map = torch.zeros((self.size, self.size))

        if door_window_nodes:
            for node in door_window_nodes:
                h = node["height_map"]
                xsize, ysize = h.size()
                xmin = math.floor(node["bbox_min"][0])
                ymin = math.floor(node["bbox_min"][2])
                if xmin < 0:
                    xmin = 0
                if ymin < 0:
                    ymin = 0
                if xsize == 256:
                    xmin = 0
                if ysize == 256:
                    ymin = 0
                to_add = torch.zeros((self.size, self.size))
                to_add[xmin : xmin + xsize, ymin : ymin + ysize] = h
                update = to_add > self.height_map
                self.height_map[update] = to_add[update]
                self.wall_mask[to_add > 0] = 1
                to_add[to_add > 0] = 0.5
                if node["category"] == "door":
                    self.door_map = self.door_map + to_add
                else:
                    self.window_map = self.window_map + to_add

    def get_transformation(self, transform):
        """
        Bad naming, really just getting the sin and cos of the
        angle of rotation.
        """
        a = transform[0]
        b = transform[8]
        scale = (a**2 + b**2) ** 0.5
        return (b / scale, a / scale)

    def add_height_map(self, to_add, category, sin, cos):
        """
        Add a new object to the composite.
        Height map, category, and angle of rotation are
        all the information required.
        """
        update = to_add > self.height_map
        self.height_map[update] = to_add[update]
        mask = torch.zeros(to_add.size())
        mask[to_add > 0] = 0.5
        self.cat_map[category] = self.cat_map[category] + mask
        self.sin_map[update] = (sin + 1) / 2
        self.cos_map[update] = (cos + 1) / 2

    def add_node(self, node):
        """
        Add a new object to the composite.
        Computes the necessary information and calls
        add_height_map
        """
        h = node["height_map"]
        category = node["category"]
        xsize, ysize = h.shape
        xmin = math.floor(node["bbox_min"][0])
        ymin = math.floor(node["bbox_min"][2])
        if xmin < 0:
            xmin = 0
        if ymin < 0:
            ymin = 0
        if xsize == 256:
            xmin = 0
        if ysize == 256:
            ymin = 0
        to_add = torch.zeros((self.size, self.size))
        to_add[xmin : xmin + xsize, ymin : ymin + ysize] = h
        sin, cos = self.get_transformation(node["transform"])
        self.add_height_map(to_add, category, sin, cos)

    def add_nodes(self, nodes):
        for node in nodes:
            self.add_node(node)

    # Use these to build a composite that renders OBBs insted of full object geometry
    def add_node_obb(self, node):
        h = node["height_map_obb"]
        category = node["category"]
        xsize, ysize = h.shape
        # xmin = math.floor(node["bbox_min_obb"][0])
        # ymin = math.floor(node["bbox_min_obb"][2])
        xmin = max(0, math.floor(node["bbox_min_obb"][0]))
        ymin = max(0, math.floor(node["bbox_min_obb"][2]))
        to_add = torch.zeros((self.size, self.size))
        # ##### TEST
        # tmp = to_add[xmin:xmin+xsize,ymin:ymin+ysize]
        # if tmp.size()[0] != h.shape[0] or tmp.size()[1] != h.shape[1]:
        #     print('------------')
        #     print(tmp.size())
        #     print(h.shape)
        #     print(f'{xmin}, {ymin}')
        # #####
        to_add[xmin : xmin + xsize, ymin : ymin + ysize] = h
        sin, cos = self.get_transformation(node["transform"])
        self.add_height_map(to_add, category, sin, cos)

    def add_nodes_obb(self, nodes):
        for node in nodes:
            self.add_node_obb(node)

    def get_cat_map(self):
        return self.cat_map.clone()

    def add_and_get_composite(
        self, to_add, category, sin, cos, num_extra_channels=1, temporary=True
    ):
        """
        Sometimes we need to create a composite to test
        if some objects should be added, without actually
        fixing the object to the scene. This method allows doing so.
        See get_composite.
        """
        if not temporary:
            raise NotImplementedError
        update = to_add > self.height_map
        mask = torch.zeros(to_add.size())
        mask[to_add > 0] = 0.5
        composite = torch.zeros(
            (len(self.categories) + num_extra_channels + 8, self.size, self.size)
        )
        composite[0] = self.room_mask
        composite[1] = self.wall_mask
        composite[2] = self.cat_map.sum(0) + mask
        composite[3] = self.height_map
        composite[3][update] = to_add[update]
        composite[4] = self.sin_map
        composite[4][update] = (sin + 1) / 2
        composite[5] = self.cos_map
        composite[5][update] = (cos + 1) / 2
        composite[6] = self.door_map
        composite[7] = self.window_map
        for i in range(len(self.categories)):
            composite[i + 8] = self.cat_map[i]
        composite[8 + category] += mask

        return composite

    def get_composite(self, num_extra_channels=1, ablation=None):
        """
        Create the actual multi-channel representation.
        Which is a N x img_size x img_size tensor.
        See the paper for more information.
        Current channel order:
            -0: room mask
            -1: wall mask
            -2: object mask
            -3: height map
            -4, 5: sin and cos of the angle of rotation
            -6, 7: single category channel for door and window
            -8~8+C: single category channel for all other categories
        Parameters
        ----------
        num_extra_channels (int, optional): number of extra empty
            channels at the end. 1 for most tasks, 0 for should continue
        ablation (string or None, optional): if set, return a subset of all
            the channels for ablation study, see the paper for more details
        """
        if ablation is None:
            composite = torch.zeros(
                (len(self.categories) + num_extra_channels + 8, self.size, self.size)
            )
            composite[0] = self.room_mask
            composite[1] = self.wall_mask
            composite[2] = self.cat_map.sum(0)
            composite[3] = self.height_map
            composite[4] = self.sin_map
            composite[5] = self.cos_map
            composite[6] = self.door_map
            composite[7] = self.window_map
            for i in range(len(self.categories)):
                composite[i + 8] = self.cat_map[i]
        elif ablation == "depth":
            composite = torch.zeros((1 + num_extra_channels, self.size, self.size))
            composite[0] = self.height_map
        elif ablation == "basic":
            composite = torch.zeros((6 + num_extra_channels, self.size, self.size))
            composite[0] = self.room_mask
            composite[1] = self.wall_mask
            composite[2] = self.cat_map.sum(0)
            composite[3] = self.height_map
            composite[4] = self.sin_map
            composite[5] = self.cos_map
        else:
            raise NotImplementedError

        return composite


if __name__ == "__main__":
    from . import ProjectionGenerator
    import scipy.misc as m

    pgen = ProjectionGenerator()
    a = RenderedScene(5, load_objects=True, data_dir="dining_final")
    c = a.create_composite()

    for node in a.object_nodes:
        c.add_node(node)

    img = c.get_composite()
    img = img[7].numpy()
    img = m.toimage(img, cmin=0, cmax=1)
    img.show()
