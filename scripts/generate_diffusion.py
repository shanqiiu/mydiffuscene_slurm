# 
# Copyright (C) 2021 NVIDIA Corporation.  All rights reserved.
# Licensed under the NVIDIA Source Code License.
# See LICENSE at https://github.com/nv-tlabs/ATISS.
# Authors: Despoina Paschalidou, Amlan Kar, Maria Shugrina, Karsten Kreis,
#          Andreas Geiger, Sanja Fidler
# 

"""Script used for generating scenes using a previously trained model."""
import argparse
import logging
import os
import sys

import numpy as np
import torch

from training_utils import load_config
from utils import floor_plan_from_scene, export_scene, get_textured_objects_in_scene

from scene_diffusion.datasets import filter_function, get_dataset_raw_and_encoded
from scene_diffusion.datasets.threed_front import ThreedFront
from scene_diffusion.datasets.threed_future_dataset import ThreedFutureDataset
from scene_diffusion.networks import build_network
from scene_diffusion.utils import get_textured_objects, get_textured_objects_based_on_objfeats
from scene_diffusion.stats_logger import AverageAggregator

from simple_3dviz import Scene
#from simple_3dviz.window import show
from simple_3dviz.behaviours.keyboard import SnapshotOnKey, SortTriangles
from simple_3dviz.behaviours.misc import LightToCamera
from simple_3dviz.behaviours.movements import CameraTrajectory
from simple_3dviz.behaviours.trajectory import Circle
from simple_3dviz.behaviours.io import SaveFrames, SaveGif
from simple_3dviz.utils import render
import matplotlib.pyplot as plt
from pyrr import Matrix44
from utils import render as render_top2down
from utils import merge_meshes
import trimesh
import open3d as o3d

def categorical_kl(p, q):
    return (p * (np.log(p + 1e-6) - np.log(q + 1e-6))).sum()

def main(argv):
    parser = argparse.ArgumentParser(
        description="Generate scenes using a previously trained model"
    )

    parser.add_argument(
        "config_file",
        help="Path to the file that contains the experiment configuration"
    )
    parser.add_argument(
        "output_directory",
        default="/tmp/",
        help="Path to the output directory"
    )
    parser.add_argument(
        "path_to_pickled_3d_futute_models",
        help="Path to the 3D-FUTURE model meshes"
    )
    parser.add_argument(
        "--path_to_floor_plan_textures",
        default="../demo/floor_plan_texture_images",
        help="Path to floor texture images"
    )
    parser.add_argument(
        "--weight_file",
        default=None,
        help="Path to a pretrained model"
    )
    parser.add_argument(
        "--n_sequences",
        default=10,
        type=int,
        help="The number of sequences to be generated"
    )
    parser.add_argument(
        "--background",
        type=lambda x: list(map(float, x.split(","))),
        default="1,1,1,1",
        help="Set the background of the scene"
    )
    parser.add_argument(
        "--up_vector",
        type=lambda x: tuple(map(float, x.split(","))),
        default="0,1,0",
        help="Up vector of the scene"
    )
    parser.add_argument(
        "--camera_position",
        type=lambda x: tuple(map(float, x.split(","))),
        default="-0.10923499,1.9325259,-7.19009",
        help="Camer position in the scene"
    )
    parser.add_argument(
        "--camera_target",
        type=lambda x: tuple(map(float, x.split(","))),
        default="0,0,0",
        help="Set the target for the camera"
    )
    parser.add_argument(
        "--window_size",
        type=lambda x: tuple(map(int, x.split(","))),
        default="512,512",
        help="Define the size of the scene and the window"
    )
    parser.add_argument(
        "--with_rotating_camera",
        action="store_true",
        help="Use a camera rotating around the object"
    )
    parser.add_argument(
        "--save_frames",
        help="Path to save the visualization frames to"
    )
    parser.add_argument(
        "--n_frames",
        type=int,
        default=360,
        help="Number of frames to be rendered"
    )
    parser.add_argument(
        "--without_screen",
        action="store_true",
        help="Perform no screen rendering"
    )
    parser.add_argument(
        "--scene_id",
        default=None,
        help="The scene id to be used for conditioning"
    )
    parser.add_argument(
        "--render_top2down",
        action="store_true",
        help="Perform top2down orthographic rendering"
    )
    parser.add_argument(
        "--without_floor",
        action="store_true",
        help="if remove the floor plane"
    )
    parser.add_argument(
        "--no_texture",
        action="store_true",
        help="if remove the texture"
    )
    parser.add_argument(
        "--save_mesh",
        action="store_true",
        help="if save mesh"
    )
    parser.add_argument(
        "--mesh_format",
        type=str,
        default=".ply",
        help="mesh format "
    )
    parser.add_argument(
        "--clip_denoised",
        action="store_true",
        help="if clip_denoised"
    )
    #
    parser.add_argument(
        "--retrive_objfeats",
        action="store_true",
        help="if retrive most similar objectfeats"
    )
    parser.add_argument(
        "--combine_size",
        action="store_true",
        help="if retrive most similar decoded surfaces"
    )
    parser.add_argument(
        "--fix_order",
        action="store_true",
        help="if use fix order"
    )
    args = parser.parse_args(argv)

    # Disable trimesh's logger
    logging.getLogger("trimesh").setLevel(logging.ERROR)

    if torch.cuda.is_available():
        device = torch.device("cuda:0")
    else:
        device = torch.device("cpu")
    print("Running code on", device)

    # Check if output directory exists and if it doesn't create it
    if not os.path.exists(args.output_directory):
        os.makedirs(args.output_directory)

    config = load_config(args.config_file)

    raw_dataset, train_dataset = get_dataset_raw_and_encoded(
        config["data"],
        filter_fn=filter_function(
            config["data"],
            split=config["training"].get("splits", ["train", "val"])
        ),
        split=config["training"].get("splits", ["train", "val"])
    )

    # Build the dataset of 3D models
    objects_dataset = ThreedFutureDataset.from_pickled_dataset(
        args.path_to_pickled_3d_futute_models
    )
    print("Loaded {} 3D-FUTURE models".format(len(objects_dataset)))

    raw_dataset, dataset = get_dataset_raw_and_encoded(
        config["data"],
        filter_fn=filter_function(
            config["data"],
            split=config["validation"].get("splits", ["test"])
        ),
        split=config["validation"].get("splits", ["test"])
    )
    print("Loaded {} scenes with {} object types:".format(
        len(dataset), dataset.n_object_types)
    )
    network, _, _ = build_network(
        dataset.feature_size, dataset.n_classes,
        config, args.weight_file, device=device
    )
    network.eval()

    # Create the scene and the behaviour list for simple-3dviz
    # scene = Scene(size=args.window_size)
    # scene.up_vector = args.up_vector
    # scene.camera_target = args.camera_target
    # scene.camera_position = args.camera_position
    # scene.light = args.camera_position

    # Create the scene and the behaviour list for simple-3dviz top-down orthographic rendering, the arguments are same as preprocess_data.py
    if args.render_top2down:
        if args.without_floor:
            scene_top2down = Scene(size=(256, 256), background=[1,1,1,1])
        else:
            scene_top2down = Scene(size=(256, 256), background=[0,0,0,1])
        scene_top2down.up_vector = (0,0,-1)
        scene_top2down.camera_target = (0, 0, 0)
        scene_top2down.camera_position = (0,4,0)
        scene_top2down.light = (0,4,0)
        scene_top2down.camera_matrix = Matrix44.orthogonal_projection(
            left=-3.1, right=3.1,
            bottom=3.1, top=-3.1,
            near=0.1, far=6
        )

    print('init scene top2donw')
    given_scene_id = None
    if args.scene_id:
        for i, di in enumerate(raw_dataset):
            if str(di.scene_id) == args.scene_id:
                given_scene_id = i

    classes = np.array(dataset.class_labels)
    print('class labels:', classes, len(classes))
    for i in range(args.n_sequences):
        if args.fix_order:
            if i < len(dataset):
                scene_idx = given_scene_id or i
            else:
                scene_idx = given_scene_id or (i % len(dataset))
        else:
            scene_idx = given_scene_id or np.random.choice(len(dataset))
            
        current_scene = raw_dataset[scene_idx]
        samples = dataset[scene_idx]
        print("{} / {}: Using the {} floor plan of scene {}".format(
            i, args.n_sequences, scene_idx, current_scene.scene_id)
        )
        # Get a floor plan
        floor_plan, tr_floor, room_mask = floor_plan_from_scene(
            current_scene, args.path_to_floor_plan_textures, no_texture=args.no_texture
        )

        if not config["validation"]["gen_traj"]:        
            bbox_params = network.generate_layout(
                    room_mask=room_mask.to(device),
                    num_points=config["network"]["sample_num_points"],
                    point_dim=config["network"]["point_dim"],
                    text=torch.from_numpy(samples['desc_emb'])[None, :].to(device) if 'desc_emb' in samples.keys() else None,
                    #text=samples['description'] if 'description' in samples.keys() else None,
                    device=device,
                    clip_denoised=args.clip_denoised,
                    batch_seeds=torch.arange(i, i+1),
            )

            boxes = dataset.post_process(bbox_params)
            bbox_params_t = torch.cat([
                boxes["class_labels"],
                boxes["translations"],
                boxes["sizes"],
                boxes["angles"]
            ], dim=-1).cpu().numpy()
            print('Generated bbox:', bbox_params_t.shape)


            if args.retrive_objfeats:
                objfeats = boxes["objfeats"].cpu().numpy()
                print('shape retrieval based on obj latent feats')

                renderables, trimesh_meshes, model_jids = get_textured_objects_based_on_objfeats(
                    bbox_params_t, objects_dataset, classes, diffusion=True, no_texture=args.no_texture, query_objfeats=objfeats, combine_size=args.combine_size, 
                )
                renderables_onlysize, trimesh_meshes_onlysize, model_jids_onlysize = get_textured_objects(
                    bbox_params_t, objects_dataset, classes, diffusion=True, no_texture=args.no_texture
                )
            else:
                renderables, trimesh_meshes, model_jids = get_textured_objects(
                    bbox_params_t, objects_dataset, classes, diffusion=True, no_texture=args.no_texture
                )


            if not args.without_floor:
                renderables += floor_plan
                trimesh_meshes += tr_floor

            if args.render_top2down:
                path_to_image = "{}/{}_{}_{:03d}.png".format(
                    args.output_directory,
                    current_scene.scene_id,
                    scene_idx,
                    i
                )
                render_top2down(
                    scene_top2down,
                    renderables,
                    color=None,
                    mode="shading",
                    frame_path=path_to_image,
                )
                
                if args.retrive_objfeats:
                    # save results of only retrieving sizes
                    path_to_image_onlysize = "{}/{}".format(
                        args.output_directory,
                        "retrive_only_size",
                    )
                    if not os.path.exists(path_to_image_onlysize):
                        os.mkdir(path_to_image_onlysize)
                    path_to_image_onlysize = "{}/{}/{}_{}_{:03d}.png".format(
                        args.output_directory,
                        "retrive_only_size",
                        current_scene.scene_id,
                        scene_idx,
                        i
                    )
                    render_top2down(
                        scene_top2down,
                        renderables_onlysize,
                        color=None,
                        mode="shading",
                        frame_path=path_to_image_onlysize,
                    )

            if args.save_mesh:
                if trimesh_meshes is not None:
                    # Create a trimesh scene and export it
                    path_to_objs = os.path.join(
                        args.output_directory,
                        "scene_mesh",
                    )
                    if not os.path.exists(path_to_objs):
                        os.mkdir(path_to_objs)
                    filename = "{}_{}_{:03d}".format(current_scene.scene_id, scene_idx, i)
                    path_to_scene = os.path.join(path_to_objs, filename+args.mesh_format)
                    whole_scene_mesh = merge_meshes( trimesh_meshes )
                    o3d.io.write_triangle_mesh(path_to_scene, whole_scene_mesh)

                if args.retrive_objfeats:
                    if trimesh_meshes_onlysize is not None:
                        # Create a trimesh scene and export it
                        path_to_objs_retrive_onlysize = os.path.join(
                            args.output_directory,
                            "scene_mesh_retrive_onlysize",
                        )
                        if not os.path.exists(path_to_objs_retrive_onlysize):
                            os.mkdir(path_to_objs_retrive_onlysize)

                        filename = "{}_{}_{:03d}".format(current_scene.scene_id, scene_idx, i)
                        path_to_scene_onlysize = os.path.join(path_to_objs_retrive_onlysize, filename+args.mesh_format)
                        whole_scene_mesh_onlysize = merge_meshes( trimesh_meshes_onlysize )
                        o3d.io.write_triangle_mesh(path_to_scene_onlysize, whole_scene_mesh_onlysize)


            if "description" in samples.keys():
                path_to_texts = os.path.join(
                    args.output_directory,
                    "{}_{}_{:03d}_text.txt".format(current_scene.scene_id, scene_idx, i)
                )
                print('the length of samples[description]: {:d}'.format( len(samples['description']) ) )
                print('text description {}'.format( samples['description']) )
                open(path_to_texts, 'w').write( ''.join(samples['description']) )

        else:
            # generate trajectory:
            bbox_params_traj = network.generate_boxes_progressive(
                room_mask=room_mask.to(device),
                num_points=config["network"]["sample_num_points"],
                point_dim=config["network"]["point_dim"],
                text=samples['description'] if 'description' in samples.keys() else None,
                device=device,
                clip_denoised=args.clip_denoised,
                batch_seeds=torch.arange(i, i+1),
                ret_traj=True,
                num_step=config["validation"]["num_step"],
            )
            for k_time, v_time in bbox_params_traj.items():
                print("{} / {} / time {}: Using the {} floor plan of scene {}".format(
                    i, args.n_sequences, k_time, scene_idx, current_scene.scene_id)
                )
                bbox_params = bbox_params_traj[k_time]
                boxes = dataset.post_process(bbox_params)
                bbox_params_t = torch.cat([
                    boxes["class_labels"],
                    boxes["translations"],
                    boxes["sizes"],
                    boxes["angles"]
                ], dim=-1).cpu().numpy()
                bbox_params_traj[k_time] = bbox_params_t

                if args.retrive_objfeats:
                    objfeats = boxes["objfeats"].cpu().numpy()
                    renderables, trimesh_meshes = get_textured_objects_based_on_objfeats(
                        bbox_params_t, objects_dataset, classes, diffusion=True, no_texture=args.no_texture,  query_objfeats=objfeats,  combine_size=args.combine_size,
                    )
                else:
                    renderables, trimesh_meshes = get_textured_objects(
                        bbox_params_t, objects_dataset, classes, diffusion=True, no_texture=args.no_texture
                    )
                    
                if not args.without_floor:
                    renderables += floor_plan
                    trimesh_meshes += tr_floor

                if args.render_top2down:
                    # Do the rendering
                    path_to_image = "{}/{}_{}_{:03d}_time{:04d}.png".format(
                        args.output_directory,
                        current_scene.scene_id,
                        scene_idx,
                        i,
                        k_time
                    )
                    render_top2down(
                        scene_top2down,
                        renderables,
                        color=None,
                        mode="shading",
                        frame_path=path_to_image,
                    )


                if args.save_mesh:
                    if trimesh_meshes is not None:
                        # Create a trimesh scene and export it
                        path_to_objs = os.path.join(
                            args.output_directory,
                            "scene_mesh"
                        )
                        if not os.path.exists(path_to_objs):
                            os.mkdir(path_to_objs)
                        filename = "{}_{}_{:03d}_time{:d}".format(current_scene.scene_id, scene_idx, i, k_time)
                        path_to_scene = os.path.join(path_to_objs, filename+args.mesh_format)
                        whole_scene_mesh = merge_meshes( trimesh_meshes )
                        o3d.io.write_triangle_mesh(path_to_scene, whole_scene_mesh)


        # generate target
        if config["validation"]["gen_gt"]:
            samples = dataset[scene_idx]

            print("{} / {} gt: Using the {} floor plan of scene {}".format(
                i, args.n_sequences, scene_idx, current_scene.scene_id)
            )
            
            # generate predicted scene objects
            print("Ground-truth shape:", samples["class_labels"].shape)
            bbox_params = {
            "class_labels": torch.from_numpy(samples["class_labels"])[None, :, ...],
            "translations": torch.from_numpy(samples["translations"])[None, :, ...],
            "sizes": torch.from_numpy(samples["sizes"])[None, :, ...],
            "angles": torch.from_numpy(samples["angles"])[None, :, ...],
            }
            if config["network"].get("objectness_dim",0) >0:
                bbox_params["objectness"] = torch.from_numpy(samples["objectness"])[None, :, ...]
            if config["network"]["objfeat_dim"] >0:
                if config["network"].get("objfeat_dim", 0) == 32:
                    bbox_params["objfeats"] = torch.from_numpy(samples["objfeats_32"])[None, :, ...]
                else:
                    bbox_params["objfeats"] = torch.from_numpy(samples["objfeats"])[None, :, ...]
            bbox_params = network.delete_empty_boxes(bbox_params, device=device)

            boxes = dataset.post_process(bbox_params)
            bbox_params_t = torch.cat([
                boxes["class_labels"],
                boxes["translations"],
                boxes["sizes"],
                boxes["angles"]
            ], dim=-1).cpu().numpy()
            
            if args.retrive_objfeats:
                objfeats = boxes["objfeats"].cpu().numpy()
                renderables, trimesh_meshes = get_textured_objects_based_on_objfeats(
                    bbox_params_t, objects_dataset, classes, diffusion=True, no_texture=args.no_texture,  query_objfeats=objfeats, combine_size=args.combine_size, 
                )
            else:
                renderables, trimesh_meshes = get_textured_objects(
                    bbox_params_t, objects_dataset, classes, diffusion=True, no_texture=args.no_texture 
                )
            if not args.without_floor:
                renderables += floor_plan
                trimesh_meshes += tr_floor


            if args.render_top2down:
                path_to_image = "{}/{}_{}_{:03d}_gt.png".format(
                    args.output_directory,
                    current_scene.scene_id,
                    scene_idx,
                    i
                )
                render_top2down(
                    scene_top2down,
                    renderables,
                    color=None,
                    mode="shading",
                    frame_path=path_to_image,
                )

            if args.save_mesh:
                if trimesh_meshes is not None:
                    # Create a trimesh scene and export it
                    path_to_objs = os.path.join(
                        args.output_directory,
                        "scene_mesh"
                    )
                    if not os.path.exists(path_to_objs):
                        os.mkdir(path_to_objs)
                    filename = "{}_{}_{:03d}_gt".format(current_scene.scene_id, scene_idx, i)
                    path_to_scene = os.path.join(path_to_objs, filename+args.mesh_format)
                    whole_scene_mesh = merge_meshes( trimesh_meshes )
                    o3d.io.write_triangle_mesh(path_to_scene, whole_scene_mesh)

           

if __name__ == "__main__":
    main(sys.argv[1:])