//
//  CGVirtualDisplayPrivate.h
//  VirtualDisplayExp
//
//  Created by Khaos Tian on 2/17/21.
//
//  Copyright (c) 2021 Khaos Tian
//  Copyright (c) 2022 Bastian Andelefski (DeskPad, https://github.com/Stengo/DeskPad)
//
//  Permission is hereby granted, free of charge, to any person obtaining a copy
//  of this software and associated documentation files (the "Software"), to deal
//  in the Software without restriction, including without limitation the rights
//  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
//  copies of the Software, and to permit persons to whom the Software is
//  furnished to do so, subject to the following conditions:
//
//  The above copyright notice and this permission notice shall be included in all
//  copies or substantial portions of the Software.
//
//  Private CoreGraphics virtual-display SPI. Not a public Apple API — it can
//  break on any macOS update. Shipped here as an experimental, opt-in companion.
//

#import <Cocoa/Cocoa.h>
#import <CoreGraphics/CoreGraphics.h>

NS_ASSUME_NONNULL_BEGIN

@class CGVirtualDisplayDescriptor;

@interface CGVirtualDisplayMode : NSObject

@property(readonly, nonatomic) CGFloat refreshRate;
@property(readonly, nonatomic) NSUInteger width;
@property(readonly, nonatomic) NSUInteger height;

- (instancetype)initWithWidth:(NSUInteger)arg1 height:(NSUInteger)arg2 refreshRate:(CGFloat)arg3;

@end

@interface CGVirtualDisplaySettings : NSObject

@property(retain, nonatomic) NSArray<CGVirtualDisplayMode *> *modes;
@property(nonatomic) unsigned int hiDPI;

- (instancetype)init;

@end

@interface CGVirtualDisplay : NSObject

@property(readonly, nonatomic) NSArray *modes; // @synthesize modes=_modes;
@property(readonly, nonatomic) unsigned int hiDPI; // @synthesize hiDPI=_hiDPI;
@property(readonly, nonatomic) CGDirectDisplayID displayID; // @synthesize displayID=_displayID;
@property(readonly, nonatomic) id terminationHandler; // @synthesize terminationHandler=_terminationHandler;
@property(readonly, nonatomic) dispatch_queue_t queue; // @synthesize queue=_queue;
@property(readonly, nonatomic) unsigned int maxPixelsHigh; // @synthesize maxPixelsHigh=_maxPixelsHigh;
@property(readonly, nonatomic) unsigned int maxPixelsWide; // @synthesize maxPixelsWide=_maxPixelsWide;
@property(readonly, nonatomic) CGSize sizeInMillimeters; // @synthesize sizeInMillimeters=_sizeInMillimeters;
@property(readonly, nonatomic) NSString *name; // @synthesize name=_name;
@property(readonly, nonatomic) unsigned int serialNum; // @synthesize serialNum=_serialNum;
@property(readonly, nonatomic) unsigned int productID; // @synthesize productID=_productID;
@property(readonly, nonatomic) unsigned int vendorID; // @synthesize vendorID=_vendorID;

- (instancetype)initWithDescriptor:(CGVirtualDisplayDescriptor *)arg1;
- (BOOL)applySettings:(CGVirtualDisplaySettings *)arg1;

@end

@interface CGVirtualDisplayDescriptor : NSObject

@property(retain, nonatomic) dispatch_queue_t queue; // @synthesize queue=_queue;
@property(retain, nonatomic) NSString *name; // @synthesize name=_name;
@property(nonatomic) unsigned int maxPixelsHigh; // @synthesize maxPixelsHigh=_maxPixelsHigh;
@property(nonatomic) unsigned int maxPixelsWide; // @synthesize maxPixelsWide=_maxPixelsWide;
@property(nonatomic) CGSize sizeInMillimeters; // @synthesize sizeInMillimeters=_sizeInMillimeters;
@property(nonatomic) unsigned int serialNum; // @synthesize serialNum=_serialNum;
@property(nonatomic) unsigned int productID; // @synthesize productID=_productID;
@property(nonatomic) unsigned int vendorID; // @synthesize vendorID=_vendorID;
@property(copy, nonatomic) void (^terminationHandler)(id, CGVirtualDisplay*);

- (instancetype)init;
- (nullable dispatch_queue_t)dispatchQueue;
- (void)setDispatchQueue:(dispatch_queue_t)arg1;

@end

NS_ASSUME_NONNULL_END
