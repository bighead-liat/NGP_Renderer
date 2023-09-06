import numpy as np
import torch
import tinycudann as tcnn
import matplotlib.pyplot as plt
import json, time, os
from tqdm import trange, tqdm
import argparse, msgpack

parser = argparse.ArgumentParser()
parser.add_argument("--scene", type=str, default="lego", help="scene name")
parser.add_argument("--data", type = str, default = "ISCAData", help = "Name of data dir")
parser.add_argument("--steps", type=int, default = 256, help="steps of each ray")
parser.add_argument("--w", "--width", type = int, default = 800, help = "width of the image")
parser.add_argument("--h", "--height", type = int, default = 800, help = "height of the image")
parser.add_argument("--name", help = "Name Of the Output Image")
parser.add_argument("--thredhold", help = "Thredhold of new method", type = float, default = 0.2)


from camera import Camera
from renderer import render_ray
from morton import *
from utils import generate_curve, gen_normal


class DensityGrid:
    def __init__(self, grid, aabb = [[0,0,0], [1,1,1]]):
        '''
        Initialize the Density Grid
        '''
        self.grid = torch.tensor(grid, device = "cuda")
        self.aabb = torch.tensor(aabb, device = "cuda")
    
    def intersect(self, points):
        idxs = torch.sum(
            torch.floor(
                (points - self.aabb[0]) / (self.aabb[1] - self.aabb[0]) * 64) 
                * 
                torch.tensor([64 * 64, 64, 1], device = points.device
            ),dim = -1, dtype = torch.int32)
        
        # Noticed that: a point out of aabb may map to a index in [0, 128**3)
        # So we must check by this
        masks_raw = ((points >= self.aabb[0]) & (points <= self.aabb[1]))
        masks = torch.all(masks_raw, dim = 1).type(torch.int32)
        valid_idxs = idxs * masks
        return self.grid[valid_idxs]

def load_msgpack(path: str):
    print(f"Loding Msgpack from {path}")
    # Return Value
    res = {}
    # Get Morton3D Object
    
    # Set File Path
    assert (os.path.isfile(path))
    dir_name, full_file_name = os.path.split(path)
    file_name, ext_name = os.path.splitext(full_file_name)
    # Load the msgpack
    with open(path, 'rb') as f:
        unpacker = msgpack.Unpacker(f, raw = False)
        config = next(unpacker)

    # Set Model Parameters
    # Total: 12206480 Parameters
    params_binary = np.frombuffer(config["snapshot"]["params_binary"], dtype = np.float16, offset = 0)
    # Transform to torch tensor
    params_binary = torch.tensor(params_binary, dtype = torch.float32)
    # Generate Parameters Dictionary
    params = {}
    # Params for Hash Encoding Network
    ## Network Params Size: 32 * 64 + 64 * 16 = 3072
    hashenc_params_network = params_binary[:(32 * 64 + 64 * 16)]
    params_binary = params_binary[(32 * 64 + 64 * 16):]
    # Params for RGB Network
    ## Network Params Size: 32 * 64 + 64 * 64 + 64 * 16 = 7168
    rgb_params_network = params_binary[:(32 * 64 + 64 * 64 + 64 * 16)]
    params_binary = params_binary[(32 * 64 + 64 * 64 + 64 * 16):]
    # Params for Hash Encoding Grid
    ## Grid size: 12196240
    hashenc_params_grid = params_binary

    # Generate Final Parameters
    params["HashEncoding"] = torch.concat([hashenc_params_network, hashenc_params_grid,  ])
    params["RGB"] = rgb_params_network
    res["params"] = params
    # Occupancy Grid Part
    grid_raw = torch.tensor(np.clip(
        np.frombuffer(config["snapshot"]["density_grid_binary"],dtype=np.float16).astype(np.float32),
        0, 1) > 0.01, dtype = torch.int8)
    grid = torch.zeros([64, 64, 64], dtype = torch.int8)
    grid_3d = torch.zeros([128, 128, 128], dtype = torch.int8)
    x, y, z = inv_morton_naive(torch.arange(0, 128**3, 1))
    grid_3d[x,y,z] = grid_raw
    for i in range(64):
        for j in range(64):
            for k in range(64):
                idxs = torch.tensor([
                    [2*i, 2*j, 2*k],
                    [2*i + 1, 2*j, 2*k],
                    [2*i, 2*j + 1, 2*k],
                    [2*i, 2*j, 2*k + 1],
                    [2*i + 1, 2*j + 1, 2*k],
                    [2*i + 1, 2*j, 2*k + 1],
                    [2*i, 2*j + 1, 2*k + 1],
                    [2*i + 1, 2*j + 1, 2*k + 1]]
                )
                xs,ys,zs = idxs[..., 0], idxs[..., 1], idxs[..., 2]
                
                if torch.sum(grid_3d[xs, ys, zs]):
                    grid[i, j, k] = 1
    # For AABB: we only consider k = 1
    ## The Domain is [-0.5, -0.5, -0.5] to [1.5, 1.5, 1.5]
    oc_grid = DensityGrid(grid.reshape(64*64*64),[[-0.5, -0.5, -0.5], [1.5, 1.5, 1.5]])
    
    res["OccupancyGrid"] = oc_grid
    
    print("Msgpack Loaded!")
    return res

if __name__ == "__main__":
    # Deal with Arguments
    ## arguments
    args = parser.parse_args()
    ### Scene Name
    scene = args.scene
    data_dir = args.data
    DATA_PATH = f"./snapshots/{data_dir}/{scene}.msgpack"
    ### Resolution
    img_w, img_h = args.w, args.h    
    resolution = (img_w, img_h)    
    ### Steps
    NERF_STEPS = args.steps
    SQRT3 = 1.7320508075688772
    STEP_LENGTH = SQRT3 / NERF_STEPS
    ## THREDHOLD
    THREDHOLD = args.thredhold
    ## Constants
    NEAR_DISTANCE = 0.6
    FAR_DISTANCE = 2.0

    CONFIG_PATH = "./configs/base.json"

    ## Device
    DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    # Visualize Informations
    print("==========Hypoxanthine's Instant NGP==========")
    print(f"Scene: NeRF-Synthetic {scene}")
    print(f"Image: {img_w} x {img_h}")
    NAME = args.name if args.name is not None else f"FTD_{scene}"
    print(f"The output image is {NAME}.png")

    # Camera Parameters
    with open(f"./data/nerf_synthetic/{scene}/transforms_test.json", "r") as f:
        meta = json.load(f)
    m_Camera_Angle_X = float(meta["camera_angle_x"])
    m_C2W = np.array(meta["frames"][0]["transform_matrix"]).reshape(4, 4)
    
    # Load Configs and Generate Components
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    hashenc = tcnn.NetworkWithInputEncoding(
        n_input_dims = 3,
        n_output_dims = 16,
        encoding_config = config["HashEnc"],
        network_config = config["HashNet"]
    ).to(DEVICE)
    shenc = tcnn.Encoding(
        n_input_dims = 3,
        encoding_config = config["SHEnc"]
    ).to(DEVICE)
    rgb_net = tcnn.Network(
        n_input_dims = 32,
        n_output_dims = 3,
        network_config = config["RGBNet"]
    ).to(DEVICE)
    camera = Camera(resolution, m_Camera_Angle_X, m_C2W)
    
    print("==========HyperParameters==========")
    print(f"Steps: {args.steps}")
    log2_hashgrid_size = config["HashEnc"]["log2_hashmap_size"]
    print(f"Hash Grid Size: 2 ^ {log2_hashgrid_size}")
    print("AABB: (-0.5, -0.5, -0.5) ~ (1.5, 1.5, 1.5)")

    # Load Parameters
    snapshots = load_msgpack(DATA_PATH)
    hashenc.load_state_dict({"params":snapshots["params"]["HashEncoding"]})
    rgb_net.load_state_dict({"params":snapshots["params"]["RGB"]})
    grid = snapshots["OccupancyGrid"]

    
    print("==========Begin Running==========")
    pixels = camera.resolution[0] * camera.resolution[1]
    ts = torch.reshape(torch.linspace(NEAR_DISTANCE, FAR_DISTANCE, NERF_STEPS, device = DEVICE), (-1, 1))
    
    NORMAL = torch.tensor(gen_normal(NERF_STEPS), device = DEVICE)
    valid_point_counter = np.zeros((camera.resolution[0], camera.resolution[1]))
    for pixel_index in trange(0, pixels):
        ray_o = torch.from_numpy(camera.rays_o[pixel_index: pixel_index + 1]).to(DEVICE)
        ray_d = torch.from_numpy(camera.rays_d[pixel_index: pixel_index + 1]).to(DEVICE)

        """
        Naive Ray Marching
        """
        
        pts = ray_o + ts * ray_d
        occupancy = grid.intersect(pts * 2 + 0.5)
        if(torch.sum(occupancy) == 0):
            continue
        ### New Method
        
        density_curve = generate_curve(occupancy, NORMAL)
        oc = torch.where(density_curve > THREDHOLD)[0]
        ts_final = torch.cat(
            [torch.arange(
                (ts[oc[i]] - 0.5 * STEP_LENGTH).item(), (ts[oc[i]] + 0.5 * STEP_LENGTH).item(), SQRT3/1024, device = DEVICE
                )
            for i in range(oc.shape[0])], dim = -1).reshape((-1, 1))
        valid_point_counter[pixel_index//800, pixel_index%800] += ts_final.count_nonzero().detach().cpu().numpy()
        pts_final = ray_o + ts_final * ray_d
        color = torch.zeros([1, 3], dtype = torch.float32, device = DEVICE)
        opacity = torch.zeros([1, 1], dtype = torch.float32, device = DEVICE)

        hash_features = hashenc(pts_final + 0.5)
        sh_features = torch.tile(shenc((ray_d+1) / 2), (hash_features.shape[0], 1))
        features = torch.concat([hash_features, sh_features], dim = -1)

        alphas_raw = hash_features[..., 0:1]
        rgbs_raw = rgb_net(features)
        camera.image[pixel_index] = render_ray(alphas_raw, rgbs_raw, SQRT3/1024)

    # Only show image and don't show the axis
    dpi = 100
    fig = plt.figure(figsize = (img_w / dpi, img_h / dpi), dpi = dpi)
    axes = fig.add_axes([0, 0, 1, 1])
    axes.set_axis_off()
    axes.imshow(camera.image.reshape(camera.w, camera.h, 3))
    output_dir = os.path.join("outputs")
    os.makedirs(output_dir, exist_ok = True)
    
    
    plt.savefig(os.path.join(output_dir, NAME))
    print(f"Done! Image was saved to ./{output_dir}/{NAME}.png")