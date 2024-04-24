import torch
import numpy as np
from tqdm import tqdm

import osrt.utils.visualize as vis
from osrt.utils.common import mse2psnr, reduce_dict, gather_all, compute_adjusted_rand_index
from osrt.utils import nerf
from osrt.utils.common import get_rank, get_world_size

import os
import math
from collections import defaultdict

lambda_reg = 0.01

class SRTTrainer:
    def __init__(self, model, optimizer, cfg, device, out_dir, render_kwargs):
        self.model = model
        self.optimizer = optimizer
        self.config = cfg
        self.device = device
        self.out_dir = out_dir
        self.render_kwargs = render_kwargs
        if 'num_coarse_samples' in cfg['training']:
            self.render_kwargs['num_coarse_samples'] = cfg['training']['num_coarse_samples']
        if 'num_fine_samples' in cfg['training']:
            self.render_kwargs['num_fine_samples'] = cfg['training']['num_fine_samples']

    def evaluate(self, val_loader, **kwargs):
        ''' Performs an evaluation.
        Args:
            val_loader (dataloader): pytorch dataloader
        '''
        self.model.eval()
        eval_lists = defaultdict(list)

        loader = val_loader if get_rank() > 0 else tqdm(val_loader)
        sceneids = []

        for data in loader:
            sceneids.append(data['sceneid'])
            eval_step_dict = self.eval_step(data, **kwargs)

            for k, v in eval_step_dict.items():
                eval_lists[k].append(v)

        sceneids = torch.cat(sceneids, 0).cuda()
        sceneids = torch.cat(gather_all(sceneids), 0)

        print(f'Evaluated {len(torch.unique(sceneids))} unique scenes.')

        eval_dict = {k: torch.cat(v, 0) for k, v in eval_lists.items()}
        eval_dict = reduce_dict(eval_dict, average=True)  # Average across processes
        eval_dict = {k: v.mean().item() for k, v in eval_dict.items()}  # Average across batch_size
        print('Evaluation results:')
        print(eval_dict)
        return eval_dict

    def train_step(self, data, it):
        self.model.train()
        self.optimizer.zero_grad()
        loss, loss_terms = self.compute_loss(data, it)
        loss = loss.mean(0)
        loss_terms = {k: v.mean(0).item() for k, v in loss_terms.items()}
        loss.backward()
        self.optimizer.step()
        return loss.item(), loss_terms

    def compute_loss(self, data, it):
        device = self.device

        input_images = data.get('input_images').to(device)
        input_camera_pos = data.get('input_camera_pos').to(device)
        input_rays = data.get('input_rays').to(device)
        target_pixels = data.get('target_pixels').to(device)

        z, z_masks = self.model.encoder(input_images, input_camera_pos, input_rays)

        target_camera_pos = data.get('target_camera_pos').to(device)
        target_rays = data.get('target_rays').to(device)

        loss = lambda_reg * self._compute_l_slot_reg(z_masks)
        loss_terms = dict()

        pred_pixels, extras = self.model.decoder(z, z_masks, target_camera_pos, target_rays, **self.render_kwargs)

        loss = loss + ((pred_pixels - target_pixels)**2).mean((1, 2))
        loss_terms['mse'] = loss
        if 'coarse_img' in extras:
            coarse_loss = ((extras['coarse_img'] - target_pixels)**2).mean((1, 2))
            loss_terms['coarse_mse'] = coarse_loss
            loss = loss + coarse_loss

        if 'segmentation' in extras:
            pred_seg = extras['segmentation']
            true_seg = data['target_masks'].to(device).float()

            # These are not actually used as part of the training loss. 
            # We just add the to the dict to report them.
            loss_terms['ari'] = compute_adjusted_rand_index(true_seg.transpose(1, 2),
                                                            pred_seg.transpose(1, 2))

            loss_terms['fg_ari'] = compute_adjusted_rand_index(true_seg.transpose(1, 2)[:, 1:],
                                                               pred_seg.transpose(1, 2))

        return loss, loss_terms

    def _compute_l_slot_reg(self, z_masks):
        """
        Computes the regularization term L_reg as the expectation of keeping slots.
        
        Args:
            Z (torch.Tensor): A tensor of size (batch_size, K, feature_dim) representing the slots.
            
        Returns:
            torch.Tensor: A scalar tensor representing the L_reg value.
        """
        batch_size, K, _ = z_masks.size()
        
        # Calculate |Z_i| for each slot
        slot_norms = z_masks.norm(dim=2)  # (batch_size, K)
        
        # Sum over slots to get the expectation term
        l_reg = slot_norms.sum(dim=1)  # (batch_size)
        
        # Take the mean over the batch
        l_reg = l_reg.mean()
        
        return l_reg

    def eval_step(self, data, full_scale=False):
        with torch.no_grad():
            loss, loss_terms = self.compute_loss(data, 1000000)

        mse = loss_terms['mse']
        psnr = mse2psnr(mse)
        return {'psnr': psnr, 'mse': mse, **loss_terms}

    def render_image(self, z, camera_pos, rays, **render_kwargs):
        """
        Args:
            z [n, k, c]: set structured latent variables
            camera_pos [n, 3]: camera position
            rays [n, h, w, 3]: ray directions
            render_kwargs: kwargs passed on to decoder
        """
        batch_size, height, width = rays.shape[:3]
        rays = rays.flatten(1, 2)
        camera_pos = camera_pos.unsqueeze(1).repeat(1, rays.shape[1], 1)

        max_num_rays = self.config['data']['num_points'] * \
                self.config['training']['batch_size'] // (rays.shape[0] * get_world_size())
        num_rays = rays.shape[1]
        img = torch.zeros_like(rays)
        all_extras = []
        for i in range(0, num_rays, max_num_rays):
            img[:, i:i+max_num_rays], extras = self.model.decoder(
                z, camera_pos[:, i:i+max_num_rays], rays[:, i:i+max_num_rays],
                **render_kwargs)
            all_extras.append(extras)

        agg_extras = {}
        for key in all_extras[0]:
            agg_extras[key] = torch.cat([extras[key] for extras in all_extras], 1)
            agg_extras[key] = agg_extras[key].view(batch_size, height, width, -1)

        img = img.view(img.shape[0], height, width, 3)
        return img, agg_extras


    def visualize(self, data, mode='val'):
        self.model.eval()

        with torch.no_grad():
            device = self.device
            input_images = data.get('input_images').to(device)
            input_camera_pos = data.get('input_camera_pos').to(device)
            input_rays = data.get('input_rays').to(device)

            camera_pos_base = input_camera_pos[:, 0]
            input_rays_base = input_rays[:, 0]

            if 'transform' in data:
                # If the data is transformed in some different coordinate system, where
                # rotating around the z axis doesn't make sense, we first undo this transform,
                # then rotate, and then reapply it.

                transform = data['transform'].to(device)
                inv_transform = torch.inverse(transform)
                camera_pos_base = nerf.transform_points_torch(camera_pos_base, inv_transform)
                input_rays_base = nerf.transform_points_torch(
                    input_rays_base, inv_transform.unsqueeze(1).unsqueeze(2), translate=False)
            else:
                transform = None

            input_images_np = np.transpose(input_images.cpu().numpy(), (0, 1, 3, 4, 2))

            z = self.model.encoder(input_images, input_camera_pos, input_rays)

            batch_size, num_input_images, height, width, _ = input_rays.shape

            num_angles = 6

            columns = []
            for i in range(num_input_images):
                header = 'input' if num_input_images == 1 else f'input {i+1}'
                columns.append((header, input_images_np[:, i], 'image'))

            if 'input_masks' in data:
                input_mask = data['input_masks'][:, 0]
                columns.append(('true seg 0°', input_mask.argmax(-1), 'clustering'))

            row_labels = None
            for i in range(num_angles):
                angle = i * (2 * math.pi / num_angles)
                angle_deg = (i * 360) // num_angles

                camera_pos_rot = nerf.rotate_around_z_axis_torch(camera_pos_base, angle)
                rays_rot = nerf.rotate_around_z_axis_torch(input_rays_base, angle)

                if transform is not None:
                    camera_pos_rot = nerf.transform_points_torch(camera_pos_rot, transform)
                    rays_rot = nerf.transform_points_torch(
                        rays_rot, transform.unsqueeze(1).unsqueeze(2), translate=False)

                img, extras = self.render_image(z, camera_pos_rot, rays_rot, **self.render_kwargs)
                columns.append((f'render {angle_deg}°', img.cpu().numpy(), 'image'))

                if 'depth' in extras:
                    depth_img = extras['depth'].unsqueeze(-1) / self.render_kwargs['max_dist']
                    depth_img = depth_img.view(batch_size, height, width, 1)
                    columns.append((f'depths {angle_deg}°', depth_img.cpu().numpy(), 'image'))

                if 'segmentation' in extras:
                    pred_seg = extras['segmentation'].cpu()
                    columns.append((f'pred seg {angle_deg}°', pred_seg.argmax(-1).numpy(), 'clustering'))
                    if i == 0:
                        ari = compute_adjusted_rand_index(
                            input_mask.flatten(1, 2).transpose(1, 2)[:, 1:],
                            pred_seg.flatten(1, 2).transpose(1, 2))
                        row_labels = ['2D Fg-ARI={:.1f}'.format(x.item() * 100) for x in ari]

            output_img_path = os.path.join(self.out_dir, f'renders-{mode}')
            vis.draw_visualization_grid(columns, output_img_path, row_labels=row_labels)

