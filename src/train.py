
import wandb
import random
import sys

import torch
import torch.optim as optim
from torch.utils.data import DataLoader
from torchvision import transforms
import time
from dataset import ImageFolder 



from models import TCM, TCMSTanH, ScaleHyperpriorStanH, GainedScaleHyperprior, gain_WACNN, WACNN_stanh
 
import os
from compressai.zoo import bmshj2018_hyperprior
torch.backends.cudnn.deterministic=True
torch.backends.cudnn.benchmark=False
from training.loss import RateDistortionLoss
from utils.helper import  CustomDataParallel, configure_annealings, configure_latent_space_policy, create_savepath
from utils.optimizer import configure_optimizers
from utils.parser import parse_args
from utils.plotting import plot_sos, plot_rate_distorsion
from training.step import train_one_epoch, test_epoch, compress_with_ac
from PIL import Image
from torch.utils.data import Dataset

def sec_to_hours(seconds):
    a=str(seconds//3600)
    b=str((seconds%3600)//60)
    c=str((seconds%3600)%60)
    d=["{} hours {} mins {} seconds".format(a, b, c)]
    print(d[0])



class TestKodakDataset(Dataset):
    def __init__(self, data_dir):
        self.data_dir = data_dir
        if not os.path.exists(data_dir):
            raise Exception(f"[!] {self.data_dir} not exitd")
        self.image_path = [os.path.join(self.data_dir,f) for f in os.listdir(self.data_dir)]

    def __getitem__(self, item):
        image_ori = self.image_path[item]
        image = Image.open(image_ori).convert('RGB')
        #transform = transforms.Compose([transforms.CenterCrop(256), transforms.ToTensor()])
        transform = transforms.Compose([transforms.ToTensor()])
        return transform(image)

    def __len__(self):
        return len(self.image_path)


def delete_keys(state_dict):
    #del state_dict["entropy_bottleneck._cdf_length"]
    #del state_dict["entropy_bottleneck._quantized_cdf"]
    #del state_dict["entropy_bottleneck._offset"]

    del state_dict["gaussian_conditional._cdf_length"]
    del state_dict["gaussian_conditional._quantized_cdf"]
    del state_dict["gaussian_conditional._offset"]
    del state_dict["gaussian_conditional.scale_table"]

    return state_dict


def save_checkpoint(state, is_best, filename,filename_best,very_best,epoch):


    if is_best:
        torch.save(state, filename_best)
        torch.save(state, very_best)
        if epoch > 150:
            wandb.save(very_best)
    else:
        torch.save(state, filename)



def get_model(args,device):

    if args.model == "wacnn_stanh":
        gaussian_configuration = configure_latent_space_policy(args,multi = True)
        annealing_strategy_gaussian =  configure_annealings(gaussian_configuration[0])
        factorized_configuration = None 
        annealing_strategy_factorized = None     
        net = WACNN_stanh( N = args.N, 
                          M = args.M,
                          multiple_decoder = args.multiple_decoder,
                          gaussian_configuration=gaussian_configuration, 
                          lambda_list = args.lambda_list )
        net = net.to(device)
        return net, gaussian_configuration, annealing_strategy_gaussian, factorized_configuration, annealing_strategy_factorized

    elif args.model == "stanh":
        gaussian_configuration = configure_latent_space_policy(args)
        annealing_strategy_gaussian =  configure_annealings(gaussian_configuration)

        factorized_configuration = gaussian_configuration
        annealing_strategy_factorized = configure_annealings(gaussian_configuration)


        net = TCMSTanH(lmbda = args.lambda_list,gaussian_configuration=gaussian_configuration,config=[2,2,2,2,2,2], head_dim=[8, 16, 32, 32, 16, 8], drop_path_rate=0.0, N=args.N, M=320)
        net = net.to(device)
        return net, gaussian_configuration, annealing_strategy_gaussian
    elif args.model == "scale_stanh":
        gaussian_configuration = configure_latent_space_policy(args)
        annealing_strategy_gaussian =  configure_annealings(gaussian_configuration)
        factorized_configuration = gaussian_configuration
        annealing_strategy_factorized = configure_annealings(gaussian_configuration)

        net = ScaleHyperpriorStanH(N = args.N, M = args.M,gaussian_configuration=gaussian_configuration)
        net = net.to(device)
        #net.update()

        if args.quality != 0:

            base_m = bmshj2018_hyperprior(quality=args.quality, pretrained=True).eval().to(device)
            base_m.update()
            state_dict = base_m.state_dict()

            state_dict = delete_keys(state_dict)

            #net.update(force = True) 
            net.load_state_dict(state_dict=state_dict, strict=False)

        return net, gaussian_configuration, annealing_strategy_gaussian, factorized_configuration, annealing_strategy_factorized
    elif args.model == "scale_gain":
        gaussian_configuration = None,
        annealing_strategy_gaussian = None 
        factorized_configuration = None 
        annealing_strategy_factorized = None 
        net = GainedScaleHyperprior(N = args.N, M = args.M, lmbda_list = args.lambda_list) 
        net = net.to(device)
        return net, gaussian_configuration, annealing_strategy_gaussian, factorized_configuration, annealing_strategy_factorized

    elif args.model == "wacnn_gain":
        gaussian_configuration = None,
        annealing_strategy_gaussian = None 
        factorized_configuration = None 
        annealing_strategy_factorized = None 
        net = gain_WACNN(N = args.N, M = args.M, lmbda_list= args.lambda_list )
        net = net.to(device)
        return net, gaussian_configuration, annealing_strategy_gaussian, factorized_configuration, annealing_strategy_factorized
    
    else:
        gaussian_configuration = None
        annealing_strategy_gaussian = None
        net = TCM(config=[2,2,2,2,2,2], head_dim=[8, 16, 32, 32, 16, 8], drop_path_rate=0.0, N=args.N, M=320)
        net = net.to(device)

        return net, gaussian_configuration, annealing_strategy_gaussian

def main(argv):


    args = parse_args(argv)

    wandb_name = args.wandb_name
    wandb.init(project= wandb_name, entity="albipresta") 

    for arg in vars(args):
        print(arg, ":", getattr(args, arg))
    type = args.type

    
    save_path = os.path.join(args.save_path, str(args.lambda_list[-1]))
    if not os.path.exists(save_path):
        os.makedirs(save_path)

    if args.seed is not None:
        torch.manual_seed(args.seed)
        random.seed(args.seed)


    train_transforms = transforms.Compose(
        [transforms.RandomCrop(args.patch_size), transforms.ToTensor()]
    )

    test_transforms = transforms.Compose(
        [transforms.CenterCrop(args.patch_size), transforms.ToTensor()]
    )

    


    psnr_res = {}
    bpp_res = {}

    if args.freeze:
        bpp_res["our"] = [0.3055]
        psnr_res["our"] = [32.529]
    else:
        bpp_res["our"] = [100]
        psnr_res["our"] = [0]

    psnr_res["base"] =   [32.529, 30.57, 29.99]
    bpp_res["base"] =  [0.3055,0.198, 0.161]  


    train_dataset = ImageFolder(args.dataset,num_images = args.num_images, split="train", transform=train_transforms)
    valid_dataset = ImageFolder(args.dataset, num_images = args.num_images_val, split="test", transform=test_transforms)
    test_dataset = TestKodakDataset(data_dir="/scratch/dataset/kodak")

    filelist = test_dataset.image_path
    device = "cuda" 



    train_dataloader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        shuffle=True,
        pin_memory=(device == "cuda"),
    )

    valid_dataloader = DataLoader(
        valid_dataset,
        batch_size=args.test_batch_size,
        num_workers=args.num_workers,
        shuffle=False,
        pin_memory=(device == "cuda"),
    )

    test_dataloader = DataLoader(
        test_dataset,
        batch_size=1,
        num_workers=args.num_workers,
        shuffle=False,
        pin_memory=(device == "cuda"),
    )


    net, _, annealing_strategy_gaussian, _, annealing_strategy_factorized = get_model(args,device)

    annealing_strategy_factorized = None

    if args.cuda and torch.cuda.device_count() > 1:
        net = CustomDataParallel(net)

    optimizer, aux_optimizer = configure_optimizers(net, args) #ffffff
    milestones = args.lr_epoch
    print("milestones: ", milestones)
    #lr_scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones, gamma=0.1)
    #lr_scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[50,80], gamma=0.5)
    lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, "min", factor=0.5, patience=4)

    criterion = RateDistortionLoss(lmbda=args.lambda_list, type=type)

    last_epoch = 0
    if args.checkpoint != "none":  # load from previous checkpoint
        print("Loading", args.checkpoint)
        checkpoint = torch.load(args.checkpoint, map_location=device)
        net.load_state_dict(checkpoint["state_dict"], strict = False)
        #if args.continue_train:
        #    last_epoch = checkpoint["epoch"] + 1
        #    optimizer.load_state_dict(checkpoint["optimizer"])
        #    aux_optimizer.load_state_dict(checkpoint["aux_optimizer"])
        #    lr_scheduler.load_state_dict(checkpoint["lr_scheduler"])
    counter = 0
    best_loss = float("inf")

    if args.freeze:
        net.freeze()
        aux_optimizer = None
    epoch_enc = 0

    #net.gaussian_conditional.stanh.define_channels_map()
    #print("END")
    

    lambda_list = args.lambda_list 
    num_levels = len(lambda_list)



    for epoch in range(last_epoch, args.epochs):
        start = time.time()
        print(f"Learning rate: {optimizer.param_groups[0]['lr']}")
        tp = net.print_information()
        #counter, model, criterion, train_dataloader, optimizer,  epoch, clip_max_norm, type='mse', annealing_strategy = None, aux_optimizer = None, wandb_log = False
        if tp > 0:
            counter = train_one_epoch(counter, 
                                    net,
                                    lambda_list,
                                    criterion,
                                    train_dataloader,
                                    optimizer,
                                    epoch,
                                    args.clip_max_norm,
                                    aux_optimizer=aux_optimizer,
                                    annealing_strategy=annealing_strategy_gaussian,
                                    annealing_strategy_factorized = annealing_strategy_factorized,
                                    type = type,
                                    wandb_log = True)
            
        print("inizio validation!!!")
        val_loss = 0
        
        for j,p in enumerate(lambda_list):
            val_bpp, val_psnr,valid_loss = test_epoch(epoch, valid_dataloader, net,j,p, criterion, wandb_log=True, valid = True)
            test_bpp, test_psnr,loss = test_epoch(epoch, test_dataloader, net,j,p, criterion, wandb_log=True, valid = False)
            val_loss = val_loss + valid_loss 
            #test_bpp = test_bpp.clone().detach()
            bpp_res["our"].append(test_bpp)
            psnr_res["our"].append(test_psnr)
        val_loss = val_loss/len(lambda_list)
        print("fine validation")

        #test_bpp, test_psnr,loss = test_epoch(epoch, test_dataloader, net, criterion, wandb_log=True, valid = False)
        #print("test fine")

        lr_scheduler.step(val_loss)
        #lr_scheduler.step()

        is_best = val_loss < best_loss
        best_loss = min(val_loss, best_loss)
        #print("compress init")

        test_bpp = test_bpp.clone().detach().item()

        #net.update()
        #print("inizio la compressione")
        #bpp, psnr  = compress_with_ac(net, filelist, device, epoch, baseline = False, wandb_log = True)
        #print("compression results: ",bpp,"   ",psnr)

        if is_best and "stanh" in args.model: #and np.abs(test_bpp - bpp_res["our"][-1])>0.01:
            
            """
            if args.freeze:
                bpp_res["our"].append(test_bpp)
                psnr_res["our"].append(test_psnr)
            else:
                bpp_res["our"] = [test_bpp]
                psnr_res["our"]= [test_psnr]
            """
            #model, device,epoch
            for j,p in enumerate(lambda_list):
                plot_sos(net,device,epoch_enc,lv = j)
                print("finito primo plot")
                #plot_rate_distorsion(bpp_res, psnr_res,epoch_enc)
                print("finito secondo plot")
                epoch_enc +=1

        if args.checkpoint != "none":
            check = "pret"
        else:
            check = "zero"

        # creating savepath
        name_folder = check + "_" +   args.model  + "_" + str(args.N)  + "_" + str(args.symmetry) + "_" + str(args.gauss_gp)
        cartella = os.path.join(args.save_path,name_folder)


        if not os.path.exists(cartella):
            os.makedirs(cartella)
            print(f"Cartella '{cartella}' creata con successo.") 
        else:
            print(f"La cartella '{cartella}' esiste già.")



        filename, filename_best,very_best=  create_savepath(args, epoch, cartella)


        if args.save:
            save_checkpoint(
                {
                    "epoch": epoch,
                    "state_dict": net.state_dict(),
                    "args":args,
                    "loss": loss,
                    "optimizer": optimizer.state_dict(),
                    "aux_optimizer": aux_optimizer.state_dict() if aux_optimizer is not None else "none",
                    "lr_scheduler": lr_scheduler.state_dict()
                },
                is_best,
                filename,
                filename_best,
                very_best,
                epoch
            )
    


        print("log also the current leraning rate")

        log_dict = {
        "train":epoch,
        "train/leaning_rate": optimizer.param_groups[0]['lr']
        #"train/beta": annealing_strategy_gaussian.bet
        }

        wandb.log(log_dict)


        end = time.time()
        print("Runtime of the epoch:  ", epoch)
        sec_to_hours(end - start) 
        print("END OF EPOCH ", epoch)



if __name__ == "__main__":  
    main(sys.argv[1:])