import os
import numpy as np
import time
import sys

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torchvision
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data import DataLoader

from torch.autograd import Variable
import torch.nn.functional as F
import torchnet as tnt
import pandas as pd
import random

from tqdm import tqdm

from dataGenerator import nii_loader, get_patch, Generator
import sys
sys.path.append('..')
from models.modelTir3D import FCDenseNet57
sys.path.append('../..')
from helpers.helper import *
from generateCSV import GenerateCSV
from os.path import expanduser
home = expanduser("~")

def __get_whole_tumor__(data):
    return (data > 0)*(data < 4)

def __get_tumor_core__(data):
    return np.logical_or(data == 1, data == 3)

def __get_enhancing_tumor__(data):
    return data == 3

def _get_dice_score_(prediction, ground_truth):

    masks = (__get_whole_tumor__, __get_tumor_core__, __get_enhancing_tumor__)
    pred  = torch.exp(prediction)
    p     = np.uint8(np.argmax(pred.data.cpu().numpy(), axis=1))
    gt    = np.uint8(ground_truth.data.cpu().numpy())
    wt, tc, et = [2*np.sum(func(p)*func(gt)) / (np.sum(func(p)) + np.sum(func(gt))+1e-6) for func in masks]
    return wt, tc, et


nclasses = 5
confusion_meter = tnt.meter.ConfusionMeter(nclasses, normalized=True)
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

def to_one_hot(y, n_dims=None):
    """ Take integer y (tensor or variable) with n dims and convert it to 1-hot representation with n+1 dims. """
    y_tensor  = y.data if isinstance(y, Variable) else y
    y_tensor  = y_tensor.type(torch.LongTensor).view(-1, 1)
    n_dims    = n_dims if n_dims is not None else int(torch.max(y_tensor)) + 1
    y_one_hot = torch.zeros(y_tensor.size()[0], n_dims).scatter_(1, y_tensor, 1)
    y_one_hot = y_one_hot.view(*y.shape, -1)
    y_one_hot = y_one_hot.transpose(-1, 1).transpose(-1, 2)#.transpose(-1, 3) 
    return Variable(y_one_hot) if isinstance(y, Variable) else y_one_hot



def dice_loss(input,target):
    """
    input is a torch variable of size BatchxnclassesxHxW representing log probabilities for each class
    target is of the groundtruth, shoud have same size as the input
    """
    # print (target.size())
    target = to_one_hot(target, n_dims=nclasses).to(device)
    # print (target.size(), input.size())

    assert input.size() == target.size(), "Input sizes must be equal."
    assert input.dim() == 5, "Input must be a 4D Tensor."

    probs = F.softmax(input)

    num   = (probs*target).sum() + 1e-3
    den   = probs.sum() + target.sum() + 1e-3
    dice  = 2.*(num/den)
    return 1. - dice


class Trainer():

    def __init__(self, Traincsv_path = None, 
                    Validcsv_path = None,
                    data_root = None,
                    logs_root = None,
                    gradual_unfreeze = True):

        # device = "cpu"

        map_location = device

        self.T3Dnclasses = nclasses
        self.Tir3Dnet = FCDenseNet57(self.T3Dnclasses)
        ckpt_tir3D    = os.path.join(home, '.DeepBrainSeg/BestModels/Tramisu_3D_FC57_best_acc.pth.tar')
        ckpt = torch.load(ckpt_tir3D, map_location=map_location)
        self.Tir3Dnet.load_state_dict(ckpt['state_dict'])
        print ("================================== TIRNET3D Loaded =================================")
        self.Tir3Dnet = self.Tir3Dnet.to(device)

        #-------------------- SETTINGS: OPTIMIZER & SCHEDULER
        self.optimizer = optim.Adam (self.Tir3Dnet.parameters(), 
                                     lr=0.0001, betas=(0.9, 0.999), eps=1e-05, weight_decay=1e-5) 
        self.scheduler = ReduceLROnPlateau(self.optimizer, factor = 0.1, patience = 5, mode = 'min')
        self.optimizer.load_state_dict(ckpt['optimizer'])
        
        
        #-------------------- SETTINGS: LOSS
        weights   = torch.FloatTensor([0.38398745, 1.48470261, 1.,         1.61940178, 0.2092336]).to(device)
        self.loss = torch.nn.CrossEntropyLoss(weight = weights)

        self.start_epoch = 0
        self.hardmine_every = 8
        self.hardmine_iteration = 1
        self.logs_root = logs_root
        self.dataRoot = data_root
        self.Traincsv_path = Traincsv_path
        self.Validcsv_path = Validcsv_path
        self.gradual_unfreeze = gradual_unfreeze


    def train(self, nnClassCount, trBatchSize, trMaxEpoch, timestampLaunch, checkpoint):

        #---- TRAIN THE NETWORK
        sub = pd.DataFrame()
        lossMIN    = 100000
        accMax     = 0


        #---- Load checkpoint
        if checkpoint != None:
            saved_parms=torch.load(checkpoint)
            self.Tir3Dnet.load_state_dict(saved_parms['state_dict'])
            # self.optimizer.load_state_dict(saved_parms['optimizer'])
            self.start_epoch= saved_parms['epochID']
            lossMIN    = saved_parms['best_loss']
            accMax     = saved_parms['best_acc']
            print (saved_parms['confusion_matrix'])

        #---- TRAIN THE NETWORK

        timestamps = []
        losses = []
        accs = []
        wt_dice_scores=[]
        tc_dice_scores=[]
        et_dice_scores=[]

        for epochID in range (self.start_epoch, trMaxEpoch):

            if (epochID % self.hardmine_every) == (self.hardmine_every -1):
                self.Traincsv_path = GenerateCSV(self.Tir3Dnet, 
                                                   self.dataRoot, 
                                                   self.logs_root, 
                                                   iteration = self.hardmine_iteration)
                self.hardmine_iteration += 1

            #-------------------- SETTINGS: DATASET BUILDERS

            datasetTrain = Generator(csv_path = self.Traincsv_path,
                                                batch_size = trBatchSize,
                                                hardmine_every = self.hardmine_every,
                                                iteration = (1 + epochID) % self.hardmine_every)
            datasetVal  =   Generator(csv_path = self.Validcsv_path,
                                                batch_size = trBatchSize,
                                                hardmine_every = self.hardmine_every,
                                                iteration = 0)

            dataLoaderTrain = DataLoader(dataset=datasetTrain, batch_size=1, shuffle=True,  num_workers=8, pin_memory=False)
            dataLoaderVal  = DataLoader(dataset=datasetVal, batch_size=1, shuffle=True, num_workers=8, pin_memory=False)

            if self.gradual_unfreeze: 
                # Need to include this in call back, prevent optimizer reset at every epoch
                # TODO:
                self._gradual_unfreezing_(epochID)
                self.optimizer = optim.Adam (filter(lambda p: p.requires_grad, 
                                                    self.Tir3Dnet.parameters()), 
                                                    lr=0.0001, betas=(0.9, 0.999), 
                                                    eps=1e-05, weight_decay=1e-5) 
                self.scheduler = ReduceLROnPlateau(self.optimizer, factor = 0.1, 
                                                     patience = 5, mode = 'min')
            

            timestampTime = time.strftime("%H%M%S")
            timestampDate = time.strftime("%d%m%Y")
            timestampSTART = timestampDate + '-' + timestampTime


            print (str(epochID)+"/" + str(trMaxEpoch) + "---")
            self.epochTrain (self.Tir3Dnet, 
                            dataLoaderTrain, 
                            self.optimizer, 
                            self.scheduler, 
                            trMaxEpoch, 
                            nnClassCount, 
                            self.loss, 
                            trBatchSize)

            lossVal, losstensor, wt_dice_score, tc_dice_score, et_dice_score, _cm = self.epochVal (self.Tir3Dnet, 
                                                                                            dataLoaderVal, 
                                                                                            self.optimizer, 
                                                                                            self.scheduler, 
                                                                                            trMaxEpoch, 
                                                                                            nnClassCount, 
                                                                                            self.loss, 
                                                                                            trBatchSize)


            currAcc = float(np.sum(np.eye(nclasses)*_cm.conf))/np.sum(_cm.conf)
            print (_cm.conf)


            timestampTime = time.strftime("%H%M%S")
            timestampDate = time.strftime("%d%m%Y")
            launchTimestamp = timestampDate + '-' + timestampTime



            self.scheduler.step(losstensor.item())

            if lossVal < lossMIN:
                lossMIN = lossVal

                timestamps.append(launchTimestamp)
                losses.append(lossVal)
                accs.append(currAcc)
                wt_dice_scores.append(wt_dice_score)
                tc_dice_scores.append(tc_dice_score)
                et_dice_scores.append(et_dice_score)

                model_name = 'model_loss = ' + str(lossVal) + '_acc = '+str(currAcc) + '_best_loss.pth.tar'
                
                states = {'epochID': epochID + 1,
                            'state_dict': self.Tir3Dnet.state_dict(),
                            'best_acc': currAcc,
                            'confusion_matrix':_cm.conf,
                            'best_loss':lossMIN,
                            'optimizer' : self.optimizer.state_dict()}

                os.makedirs(os.path.join(self.logs_root, 'models'), exist_ok=True)
                torch.save(states, os.path.join(self.logs_root, 'models', model_name))
                print ('Epoch [' + str(epochID + 1) + '] [save] [' + launchTimestamp + '] loss= ' + str(lossVal) + ' wt_dice_score='+str(wt_dice_score)+' tc_dice_score='+str(tc_dice_score) +' et_dice_score='+str(et_dice_score))

            elif currAcc > accMax:
                accMax  = currAcc
                timestamps.append(launchTimestamp)
                losses.append(lossVal)
                accs.append(accMax)
                wt_dice_scores.append(wt_dice_score)
                tc_dice_scores.append(tc_dice_score)
                et_dice_scores.append(et_dice_score)

                model_name = 'model_loss = ' + str(lossVal) + '_acc = '+str(currAcc) + '_best_acc.pth.tar'

                states = {'epochID': epochID + 1,
                            'state_dict': self.Tir3Dnet.state_dict(),
                            'best_acc': accMax,
                            'confusion_matrix':_cm.conf,
                            'best_loss':lossVal,
                            'optimizer' : self.optimizer.state_dict()}

                os.makedirs(os.path.join(self.logs_root, 'models'), exist_ok=True)
                torch.save(states, os.path.join(self.logs_root, 'models', model_name))
                print ('Epoch [' + str(epochID + 1) + '] [save] [' + launchTimestamp + '] loss= ' + str(lossVal) + ' wt_dice_score='+str(wt_dice_score)+' tc_dice_score='+str(tc_dice_score) +' et_dice_score='+str(et_dice_score) + ' Acc = '+ str(currAcc))


            else:
                print ('Epoch [' + str(epochID + 1) + '] [----] [' + launchTimestamp + '] loss= ' + str(lossVal) + ' wt_dice_score='+str(wt_dice_score)+' tc_dice_score='+str(tc_dice_score) +' et_dice_score='+str(et_dice_score))


        sub['timestamp'] = timestamps
        sub['loss'] = losses
        sub['WT_dice_score'] = wt_dice_scores
        sub['TC_dice_score'] = tc_dice_scores
        sub['ET_dice_score'] = et_dice_scores

        sub.to_csv(os.path.join(self.logs_root, 'training.csv'), index=True)


    def _gradual_unfreezing_(self, epochID):
        nlayers = len(model.named_children())
        layer_epoch = nlayers//self.hardmine_every

        for i, (name, child) in enumerate(self.Tir3Dnet.named_children()):

            if i >= nlayers - (epochID + 1)*layer_epoch:
                print(name + ' is unfrozen')
                for param in child.parameters():
                    param.requires_grad = True
            else:
                print(name + ' is frozen')
                for param in child.parameters():
                    param.requires_grad = False


    #--------------------------------------------------------------------------------
    def _gradual_unfreezing_(self, epochID):
        nlayers = 0
        for _ in self.Tir3Dnet.named_children(): nlayers += 1

        layer_epoch = 2*nlayers//self.hardmine_every

        for i, (name, child) in enumerate(self.Tir3Dnet.named_children()):

            if i >= nlayers - (epochID + 1)*layer_epoch:
                print(name + ' is unfrozen')
                for param in child.parameters():
                    param.requires_grad = True
            else:
                print(name + ' is frozen')
                for param in child.parameters():
                    param.requires_grad = False    



    #--------------------------------------------------------------------------------
    def epochTrain (self, model, dataLoader, optimizer, scheduler, epochMax, classCount, loss, trBatchSize):

        phase='train'
        with torch.set_grad_enabled(phase == 'train'):
            for batchID, (data, seg, weight_map) in tqdm(enumerate (dataLoader)):
                
                target = torch.cat(seg).long().squeeze(0)
                data = torch.cat(data).float().squeeze(0)
                # weight_map = torch.cat(weight_map).float().squeeze(0) / torch.max(weight_map)

                varInput  = data.to(device)
                varTarget = target.to(device)
                # varMap    = weight_map.to(device)
                # print (varInput.size(), varTarget.size())

                varOutput = model(varInput)
                
                cross_entropy_lossvalue = loss(varOutput, varTarget)

                # assert False
                # cross_entropy_lossvalue = torch.mean(cross_entropy_lossvalue)
                dice_loss_ =  dice_loss(varOutput, varTarget)
                lossvalue  = cross_entropy_lossvalue + dice_loss_
                # lossvalue  = cross_entropy_lossvalue


                # print(lossvalue.size(), varOutput.size(), varMap.size())
                lossvalue = torch.mean(lossvalue)

                optimizer.zero_grad()
                lossvalue.backward()
                optimizer.step()

    #--------------------------------------------------------------------------------
    def epochVal (self, model, dataLoader, optimizer, scheduler, epochMax, classCount, loss, trBatchSize):

        model.eval ()

        lossVal = 0
        lossValNorm = 0

        losstensorMean = 0
        confusion_meter.reset()

        wt_dice_score, tc_dice_score, et_dice_score = 0.0, 0.0, 0.0
        with torch.no_grad():
            for i, (data, seg, weight_map) in enumerate(dataLoader):
                
                target = torch.cat(seg).long().squeeze(0)
                data = torch.cat(data).float().squeeze(0)
                # weight_map = torch.cat(weight_map).float().squeeze(0) / torch.max(weight_map)

                varInput  = data.to(device)
                varTarget = target.to(device)
                # varMap    = weight_map.to(device)
                # print (varInput.size(), varTarget.size())

                varOutput = model(varInput)
                _, preds = torch.max(varOutput,1)

                wt_, tc_, et_ = _get_dice_score_(varOutput, varTarget)
                wt_dice_score += wt_
                tc_dice_score += tc_
                et_dice_score += et_

                cross_entropy_lossvalue = loss(varOutput, varTarget)

                # assert False
                # cross_entropy_lossvalue = torch.mean(cross_entropy_lossvalue)
                dice_loss_              =  dice_loss(varOutput, varTarget)

                losstensor  =  cross_entropy_lossvalue + dice_loss_
                # losstensor  =  cross_entropy_lossvalue 
                # print varOutput, varTarget
                losstensorMean += losstensor
                confusion_meter.add(preds.data.view(-1), varTarget.data.view(-1))
                lossVal += losstensor.item()
                del losstensor,_,preds
                del varOutput, varTarget, varInput
                lossValNorm += 1

            wt_dice_score, tc_dice_score, et_dice_score = wt_dice_score/lossValNorm, tc_dice_score/lossValNorm, et_dice_score/lossValNorm
            outLoss = lossVal / lossValNorm
            losstensorMean = losstensorMean / lossValNorm

        return outLoss, losstensorMean, wt_dice_score, tc_dice_score, et_dice_score, confusion_meter



if __name__ == "__main__":
    trainer = Trainer('../../../../Logs/csv/training.csv',
                        '../../../../Logs/csv/validation.csv',
                        '../../../../MICCAI_BraTS2020_TrainingData',
                        '../../../../Logs')

    ckpt_path = '../../../../Logs/models/model_loss = 0.2870774023637236_acc = 0.904420656270211_best_loss.pth.tar'
    timestampTime = time.strftime("%H%M%S")
    timestampDate = time.strftime("%d%m%Y")
    timestampLaunch = timestampDate + '-' + timestampTime
    trainer.train(nnClassCount = nclasses, 
                  trBatchSize = 4, 
                  trMaxEpoch = 50, 
                  timestampLaunch = timestampLaunch, 
                  checkpoint = ckpt_path)